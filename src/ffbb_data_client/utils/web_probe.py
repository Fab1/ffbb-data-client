"""
Probing module for official FFBB web frontend Next.js chunks and origins.

Inspects the live bundles of https://www.ffbb.com to dynamically extract:
- Directus Back-Office URLs (DIRECTUS_BO_URL)
- Meilisearch public URLs (meilisearchPublicUrl)
- Static assets URLs (NEXT_PUBLIC_DIRECTUS_STATICSURL)
- Active production hostnames in bundle scripts
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import re
import time
import urllib.request
from typing import Any

from ..config import DEFAULT_USER_AGENT

logger = logging.getLogger(__name__)

OFFICIAL_FFBB_WEB_URL = "https://www.ffbb.com"

# Compiled regex patterns using triple quotes to avoid single/double quote delimiter collisions
# Support nested paths (e.g. /_next/static/chunks/pages/_app-123.js, /_next/static/chunks/app/layout-456.js)
RE_CHUNK_PATH = re.compile(r"""(/_next/static/chunks/[a-zA-Z0-9_\./-]+\.js)""")
# Support NEXT_PUBLIC_DIRECTUS_BO_URL, NEXT_PUBLIC_DIRECTUS_URL, DIRECTUS_BO_URL, and DIRECTUS_URL
RE_BO_URLS = re.compile(
    r"""(?:NEXT_PUBLIC_)?DIRECTUS_(?:BO_)?URL(?:\s*\|\|\s*|:|=|\s+)\s*["']([^"']+)["']"""
)
RE_MEILI_URL = re.compile(
    r"""(?:NEXT_PUBLIC_)?(?:meilisearchPublicUrl|MEILISEARCH_URL)\s*(?::|=|\s+)\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
RE_STATICS_URL = re.compile(
    r"""(?:NEXT_PUBLIC_)?DIRECTUS_STATICSURL(?:\s*\|\|\s*|:|=|\s+)\s*["']([^"']+)["']"""
)
RE_FFBB_HOSTS = re.compile(
    r"""https://[a-zA-Z0-9\.-]+(?:\.ffbb\.(?:com|app)|meili[a-zA-Z0-9\.-]*)"""
)

# In-memory TTL cache for probe results
_probe_cache: dict[str, Any] = {}


def clear_probe_cache() -> None:
    """Clear the in-memory web probe cache."""
    _probe_cache.clear()


def probe_ffbb_web_origins(
    web_url: str = OFFICIAL_FFBB_WEB_URL,
    timeout: float = 8.0,
    max_chunk_workers: int = 8,
    ttl: float = 3600.0,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Inspect official ffbb.com Next.js bundles to extract live backend origins.

    Args:
        web_url: Root URL of the official web application (defaults to https://www.ffbb.com).
        timeout: Socket timeout for HTTP requests in seconds.
        max_chunk_workers: Max concurrency for downloading chunk bundles.

    Returns:
        A dict containing:
        - healthy_directus_urls: list of validated, responding Directus base URLs (with trailing slash)
        - directus_bo_urls: list of all declared Directus back-office URLs
        - meilisearch_urls: list of Meilisearch URLs found in Next.js config
        - statics_urls: list of static asset URLs
        - all_ffbb_hosts: set of all detected https://*.ffbb.(com|app) domains
        - scanned_chunks_count: number of Next.js chunks examined
    """
    now = time.time()
    cached_ts = float(_probe_cache.get("timestamp", 0.0))
    if (
        not force_refresh
        and _probe_cache
        and (now - cached_ts) < ttl
        and _probe_cache.get("web_url") == web_url
    ):
        logger.debug(
            "Returning cached FFBB web probe results (age: %.1fs)",
            now - cached_ts,
        )
        return dict(_probe_cache["data"])

    proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    handlers: list[urllib.request.BaseHandler] = []
    if proxy_url:
        handlers.append(
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        )
    opener = urllib.request.build_opener(*handlers)

    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    try:
        req = urllib.request.Request(web_url, headers=headers)
        with opener.open(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception as exc:
        logger.warning("Failed to fetch official web page %s: %s", web_url, exc)
        return {
            "healthy_directus_urls": [],
            "directus_bo_urls": [],
            "meilisearch_urls": [],
            "statics_urls": [],
            "all_ffbb_hosts": [],
            "scanned_chunks_count": 0,
            "error": str(exc),
        }

    chunk_paths = list(set(RE_CHUNK_PATH.findall(html)))

    directus_bo_urls: list[str] = []
    meilisearch_urls: list[str] = []
    statics_urls: list[str] = []
    all_ffbb_hosts: set[str] = set()

    def _fetch_chunk(path: str) -> tuple[str, str]:
        chunk_url = f"{web_url.rstrip('/')}{path}"
        try:
            creq = urllib.request.Request(chunk_url, headers=headers)
            with opener.open(creq, timeout=timeout) as cresp:
                return path, cresp.read().decode("utf-8", errors="ignore")
        except Exception:
            return path, ""

    if chunk_paths:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_chunk_workers
        ) as pool:
            results = pool.map(_fetch_chunk, chunk_paths)

        for _path, content in results:
            if not content:
                continue

            bo_matches = RE_BO_URLS.findall(content)
            for m in bo_matches:
                for u in m.split(","):
                    u_clean = u.strip().rstrip("/") + "/"
                    if u_clean and u_clean not in directus_bo_urls:
                        directus_bo_urls.append(u_clean)

            meili_matches = RE_MEILI_URL.findall(content)
            for m in meili_matches:
                m_clean = m.strip().rstrip("/")
                if m_clean and m_clean not in meilisearch_urls:
                    meilisearch_urls.append(m_clean)

            statics_matches = RE_STATICS_URL.findall(content)
            for m in statics_matches:
                m_clean = m.strip().rstrip("/")
                if m_clean and m_clean not in statics_urls:
                    statics_urls.append(m_clean)

            hosts = RE_FFBB_HOSTS.findall(content)
            all_ffbb_hosts.update(hosts)

    healthy_directus_urls: list[str] = []
    for candidate in directus_bo_urls:
        cfg_url = f"{candidate}items/configuration"
        try:
            hreq = urllib.request.Request(cfg_url, headers=headers)
            with opener.open(hreq, timeout=min(5.0, timeout)) as hresp:
                if hresp.status == 200:
                    healthy_directus_urls.append(candidate)
        except Exception as exc:
            # Candidate directus back-office did not respond cleanly to health check
            logger.debug("Directus health probe failed for %s: %s", candidate, exc)

    result: dict[str, Any] = {
        "healthy_directus_urls": healthy_directus_urls,
        "directus_bo_urls": directus_bo_urls,
        "meilisearch_urls": meilisearch_urls,
        "statics_urls": statics_urls,
        "all_ffbb_hosts": sorted(all_ffbb_hosts),
        "scanned_chunks_count": len(chunk_paths),
    }

    if healthy_directus_urls or directus_bo_urls:
        _probe_cache.clear()
        _probe_cache.update({"web_url": web_url, "data": result, "timestamp": now})

    return result
