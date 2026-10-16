"""Tests for ffbb_data_client.utils.web_probe."""

from unittest.mock import MagicMock, patch

import pytest

from ffbb_data_client.utils.web_probe import clear_probe_cache, probe_ffbb_web_origins


@pytest.fixture(autouse=True)
def _reset_web_probe_cache():
    clear_probe_cache()
    yield
    clear_probe_cache()


def test_probe_ffbb_web_origins_success():
    mock_html = b"""
    <html>
      <head>
        <script src="/_next/static/chunks/app-config-123.js"></script>
        <script src="/_next/static/chunks/other-456.js"></script>
      </head>
    </html>
    """

    mock_chunk_js = b"""
    var e = {
        DIRECTUS_BO_URL: "https://bo.ffbb.com,https://bo.ffbb.app",
        meilisearchPublicUrl: "https://meilisearch-prod.ffbb.app",
        NEXT_PUBLIC_DIRECTUS_STATICSURL: "https://statics.ffbb.app",
    };
    """

    with patch("urllib.request.OpenerDirector.open") as mock_open:
        resp_html = MagicMock()
        resp_html.read.return_value = mock_html
        resp_html.__enter__.return_value = resp_html

        resp_chunk = MagicMock()
        resp_chunk.read.return_value = mock_chunk_js
        resp_chunk.__enter__.return_value = resp_chunk

        resp_health = MagicMock()
        resp_health.status = 200
        resp_health.__enter__.return_value = resp_health

        mock_open.side_effect = [
            resp_html,
            resp_chunk,
            resp_chunk,
            resp_health,
            resp_health,
        ]

        result = probe_ffbb_web_origins(web_url="https://www.ffbb.com", timeout=2.0)

        bo_urls = list(result["directus_bo_urls"])
        healthy_urls = list(result["healthy_directus_urls"])
        meili_urls = list(result["meilisearch_urls"])
        statics = list(result["statics_urls"])

        assert any(u == "https://bo.ffbb.com/" for u in bo_urls)
        assert any(u == "https://bo.ffbb.app/" for u in bo_urls)
        assert any(u == "https://bo.ffbb.com/" for u in healthy_urls)
        assert any(u == "https://meilisearch-prod.ffbb.app" for u in meili_urls)
        assert any(u == "https://statics.ffbb.app" for u in statics)
        assert result["scanned_chunks_count"] == 2


def test_probe_ffbb_web_origins_unreachable():
    with patch(
        "urllib.request.OpenerDirector.open", side_effect=Exception("Connection reset")
    ):
        result = probe_ffbb_web_origins(
            web_url="https://invalid.example.com", timeout=1.0
        )
        assert result["healthy_directus_urls"] == []
        assert result["directus_bo_urls"] == []
        assert "error" in result


def test_probe_ffbb_web_origins_nested_chunks_and_cache():
    from ffbb_data_client.utils.web_probe import clear_probe_cache

    clear_probe_cache()

    mock_html = b"""
    <html>
      <head>
        <script src="/_next/static/chunks/pages/_app-123.js"></script>
        <script src="/_next/static/chunks/app/layout-456.js"></script>
      </head>
    </html>
    """

    mock_chunk_js = b"""
    var e = {
        NEXT_PUBLIC_DIRECTUS_BO_URL: "https://bo.ffbb.com/",
        meilisearchPublicUrl: "https://meilisearch-prod.ffbb.app",
    };
    """

    with patch("urllib.request.OpenerDirector.open") as mock_open:
        resp_html = MagicMock()
        resp_html.read.return_value = mock_html
        resp_html.__enter__.return_value = resp_html

        resp_chunk = MagicMock()
        resp_chunk.read.return_value = mock_chunk_js
        resp_chunk.__enter__.return_value = resp_chunk

        resp_health = MagicMock()
        resp_health.status = 200
        resp_health.__enter__.return_value = resp_health

        mock_open.side_effect = [
            resp_html,
            resp_chunk,
            resp_chunk,
            resp_health,
        ]

        # First call: performs network probes
        res1 = probe_ffbb_web_origins(web_url="https://www.ffbb.com", timeout=2.0)
        assert "https://bo.ffbb.com/" in res1["healthy_directus_urls"]
        assert res1["scanned_chunks_count"] == 2
        assert mock_open.call_count == 4

        # Second call: served from memory cache without new network calls
        res2 = probe_ffbb_web_origins(web_url="https://www.ffbb.com", timeout=2.0)
        assert res2["healthy_directus_urls"] == res1["healthy_directus_urls"]
        assert mock_open.call_count == 4  # No additional calls

        # Force refresh: bypasses cache
        mock_open.side_effect = [
            resp_html,
            resp_chunk,
            resp_chunk,
            resp_health,
        ]
        res3 = probe_ffbb_web_origins(
            web_url="https://www.ffbb.com", timeout=2.0, force_refresh=True
        )
        assert res3["healthy_directus_urls"] == res1["healthy_directus_urls"]
        assert mock_open.call_count == 8

        clear_probe_cache()
