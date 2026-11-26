import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import requests

from app.services.arxiv_service import ArxivService


ATOM_FEED = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns='http://www.w3.org/2005/Atom'>
  <entry>
    <id>http://arxiv.org/abs/2609.99999v1</id>
    <title>Intraday alpha and liquidity</title>
    <summary>A paper about volatility and market microstructure.</summary>
    <published>__RECENT_PUBLISHED_AT__</published>
    <author><name>Test Author</name></author>
    <category term='q-fin.TR'/>
    <link title='pdf' type='application/pdf' href='https://arxiv.org/pdf/2609.99999v1.pdf'/>
  </entry>
</feed>
""".replace(
    "__RECENT_PUBLISHED_AT__",
    (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
)


class ArxivRetryTests(unittest.TestCase):
    def test_fetch_retries_transient_network_failure(self) -> None:
        settings = SimpleNamespace(
            arxiv_base_url="https://example.test/api/query",
            arxiv_max_candidates=15,
            arxiv_extra_keywords="volatility",
        )
        first = requests.Timeout("temporary timeout")
        response = Mock(text=ATOM_FEED)
        response.raise_for_status.return_value = None

        with patch("app.services.arxiv_service.requests.get", side_effect=[first, response]) as get, \
             patch("app.services.arxiv_service.time.sleep"):
            candidates = ArxivService(settings, Mock()).fetch_recent_candidates(hours_back=48)

        self.assertEqual(2, get.call_count)
        self.assertEqual(["2609.99999v1"], [item.arxiv_id for item in candidates])

    def test_fetch_reports_bounded_failure_after_retries(self) -> None:
        settings = SimpleNamespace(
            arxiv_base_url="https://example.test/api/query",
            arxiv_max_candidates=15,
            arxiv_extra_keywords="volatility",
        )
        with patch("app.services.arxiv_service.requests.get", side_effect=requests.Timeout("offline")) as get, \
             patch("app.services.arxiv_service.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "arXiv 检索失败，已请求 3 次"):
                ArxivService(settings, Mock()).fetch_recent_candidates(hours_back=48)

        self.assertEqual(3, get.call_count)

    def test_legacy_http_endpoint_is_upgraded_and_has_https_fallback(self) -> None:
        urls = ArxivService._query_urls("http://export.arxiv.org/api/query")
        self.assertEqual(
            ["https://export.arxiv.org/api/query", "https://arxiv.org/api/query"],
            urls,
        )

    def test_retry_rotates_to_official_https_fallback(self) -> None:
        settings = SimpleNamespace(
            arxiv_base_url="https://export.arxiv.org/api/query",
            arxiv_max_candidates=15,
            arxiv_extra_keywords="volatility",
        )
        response = Mock(text=ATOM_FEED)
        response.raise_for_status.return_value = None
        with patch("app.services.arxiv_service.requests.get", side_effect=[requests.Timeout("primary timeout"), response]) as get, \
             patch("app.services.arxiv_service.time.sleep"):
            candidates = ArxivService(settings, Mock()).fetch_recent_candidates(hours_back=48)

        self.assertEqual(1, len(candidates))
        self.assertEqual("https://export.arxiv.org/api/query", get.call_args_list[0].args[0])
        self.assertEqual("https://arxiv.org/api/query", get.call_args_list[1].args[0])

    def test_bad_feed_response_consumes_only_bounded_attempts(self) -> None:
        settings = SimpleNamespace(
            arxiv_base_url="https://example.test/api/query",
            arxiv_max_candidates=15,
            arxiv_extra_keywords="volatility",
        )
        response = Mock(text="not an atom feed")
        response.raise_for_status.return_value = None
        with patch("app.services.arxiv_service.requests.get", return_value=response) as get, \
             patch("app.services.arxiv_service.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "已请求 3 次"):
                ArxivService(settings, Mock()).fetch_recent_candidates(hours_back=48)

        self.assertEqual(3, get.call_count)

    def test_request_identifies_autosci_to_arxiv(self) -> None:
        settings = SimpleNamespace(
            arxiv_base_url="https://example.test/api/query",
            arxiv_max_candidates=15,
            arxiv_extra_keywords="volatility",
            arxiv_user_agent="AutoSci/test",
        )
        response = Mock(text=ATOM_FEED)
        response.raise_for_status.return_value = None
        with patch("app.services.arxiv_service.requests.get", return_value=response) as get:
            ArxivService(settings, Mock()).fetch_recent_candidates(hours_back=48)

        self.assertEqual("AutoSci/test", get.call_args.kwargs["headers"]["User-Agent"])
        self.assertEqual("application/atom+xml", get.call_args.kwargs["headers"]["Accept"])

    def test_recent_cache_survives_short_arxiv_outage(self) -> None:
        with TemporaryDirectory() as directory:
            settings = SimpleNamespace(
                arxiv_base_url="https://example.test/api/query",
                arxiv_max_candidates=15,
                arxiv_extra_keywords="volatility",
                arxiv_user_agent="AutoSci/test",
                artifacts_dir=directory,
            )
            response = Mock(text=ATOM_FEED)
            response.raise_for_status.return_value = None
            service = ArxivService(settings, Mock())
            with patch("app.services.arxiv_service.requests.get", return_value=response):
                self.assertEqual(1, len(service.fetch_recent_candidates(hours_back=48)))

            with patch("app.services.arxiv_service.requests.get", side_effect=requests.Timeout("offline")), \
                 patch("app.services.arxiv_service.time.sleep"):
                cached = service.fetch_recent_candidates(hours_back=48)

            self.assertEqual(["2609.99999v1"], [item.arxiv_id for item in cached])


if __name__ == "__main__":
    unittest.main()
