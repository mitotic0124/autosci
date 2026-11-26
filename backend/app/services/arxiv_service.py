from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
import re
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import feedparser
import requests

from app.core.config import Settings
from app.services.llm_service import LLMService


logger = logging.getLogger(__name__)

ARXIV_MAX_ATTEMPTS = 3
ARXIV_REQUEST_TIMEOUT = (5, 25)
ARXIV_RETRY_DELAYS = (2, 6)
ARXIV_MIN_REQUEST_INTERVAL_SECONDS = 3
ARXIV_REQUEST_HEADERS = {
    "Accept": "application/atom+xml",
}
ARXIV_CACHE_MAX_AGE = timedelta(hours=72)


@dataclass
class CandidatePaper:
    arxiv_id: str
    title: str
    authors: list[str]
    abstract: str
    categories: list[str]
    published_at: datetime
    url: str
    pdf_url: str


class ArxivService:
    def __init__(self, settings: Settings, llm_service: LLMService) -> None:
        self._settings = settings
        self._llm_service = llm_service
        self._last_request_at: float | None = None
        self._query_keywords = self._build_query_keywords()
        self._keywords = self._build_weighted_keywords()

    def _build_weighted_keywords(self) -> dict[str, float]:
        keywords = {
            "daily": 12.0,
            "intraday": 14.0,
            "high-frequency": 16.0,
            "high frequency": 16.0,
            "minute": 8.0,
            "factor": 10.0,
            "asset pricing": 10.0,
            "portfolio": 8.0,
            "china": 6.0,
            "market microstructure": 12.0,
            "machine learning": 8.0,
            "volatility": 10.0,
            "liquidity": 10.0,
            "limit order book": 12.0,
            "transaction cost": 9.0,
            "return predictability": 10.0,
            "cross-sectional": 9.0,
            "cross section": 9.0,
            "alpha": 8.0,
        }
        for keyword in self._parse_extra_keywords(self._settings.arxiv_extra_keywords):
            keywords.setdefault(keyword, 7.0)
        return keywords

    def _build_query_keywords(self) -> list[str]:
        base_keywords = [
            "daily",
            "intraday",
            "high frequency",
            "factor",
            "asset pricing",
            "volatility",
            "liquidity",
            "market microstructure",
            "limit order book",
            "transaction cost",
            "return predictability",
            "cross-sectional",
            "alpha",
        ]
        merged = base_keywords + self._parse_extra_keywords(self._settings.arxiv_extra_keywords)
        deduped: list[str] = []
        seen: set[str] = set()
        for keyword in merged:
            normalized = keyword.strip().lower()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)
        return deduped

    @staticmethod
    def _parse_extra_keywords(raw: str | None) -> list[str]:
        if not raw:
            return []
        return [item.strip().lower() for item in re.split(r"[,，;\n；]+", raw) if item.strip()]

    def _build_search_query(self) -> str:
        categories = "(cat:q-fin.ST OR cat:q-fin.TR OR cat:q-fin.PM OR cat:q-fin.CP OR cat:q-fin.MF)"
        keyword_query_items: list[str] = []
        for keyword in self._query_keywords:
            token = f'all:"{keyword}"' if any(ch in keyword for ch in {" ", "-", ":"}) else f"all:{keyword}"
            keyword_query_items.append(token)
        keyword_query = " OR ".join(keyword_query_items)
        return f"{categories} AND ({keyword_query})"

    def fetch_recent_candidates(self, hours_back: int = 48) -> list[CandidatePaper]:
        params = {
            "search_query": self._build_search_query(),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "start": 0,
            "max_results": self._settings.arxiv_max_candidates,
        }
        configured_url = (self._settings.arxiv_base_url or "https://export.arxiv.org/api/query").strip()
        urls = self._query_urls(configured_url)
        feed = None
        errors: list[str] = []
        for attempt in range(ARXIV_MAX_ATTEMPTS):
            url = urls[attempt % len(urls)]
            try:
                self._respect_request_interval()
                response = requests.get(
                    url,
                    params=params,
                    headers={
                        **ARXIV_REQUEST_HEADERS,
                        "User-Agent": str(getattr(self._settings, "arxiv_user_agent", "AutoSci/1.0")).strip()
                        or "AutoSci/1.0",
                    },
                    timeout=ARXIV_REQUEST_TIMEOUT,
                )
                response.raise_for_status()
                parsed_feed = feedparser.parse(response.text)
                if getattr(parsed_feed, "bozo", False) and not getattr(parsed_feed, "entries", []):
                    raise ValueError(f"arXiv returned an invalid Atom feed: {parsed_feed.bozo_exception}")
                feed = parsed_feed
                break
            except requests.RequestException as exc:
                detail = f"{url} attempt {attempt + 1}/{ARXIV_MAX_ATTEMPTS}: {exc}"
                errors.append(detail)
                logger.warning("arXiv query failed; retrying if attempts remain: %s", detail)
                delay = self._retry_delay(exc, attempt)
            except ValueError as exc:
                detail = f"{url} attempt {attempt + 1}/{ARXIV_MAX_ATTEMPTS}: {exc}"
                errors.append(detail)
                logger.warning("arXiv response was not a usable feed; retrying if attempts remain: %s", detail)
                delay = ARXIV_RETRY_DELAYS[min(attempt, len(ARXIV_RETRY_DELAYS) - 1)]
            if attempt < ARXIV_MAX_ATTEMPTS - 1:
                time.sleep(delay)

        if feed is None:
            cached = self._load_cached_candidates(hours_back)
            if cached:
                logger.warning(
                    "arXiv API unavailable after %s attempts; using %s cached candidates from the last 72 hours.",
                    ARXIV_MAX_ATTEMPTS,
                    len(cached),
                )
                return cached
            raise RuntimeError(f"arXiv 检索失败，已请求 {ARXIV_MAX_ATTEMPTS} 次：" + " | ".join(errors[-3:]))

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

        candidates: list[CandidatePaper] = []
        for entry in feed.entries:
            try:
                published = datetime.strptime(entry.published, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except (AttributeError, TypeError, ValueError):
                logger.warning("Skipping malformed arXiv entry without a valid published timestamp: %s", getattr(entry, "id", "unknown"))
                continue
            if published < cutoff:
                continue

            arxiv_id = entry.id.rsplit("/", 1)[-1]
            pdf_url = ""
            for link in entry.links:
                if getattr(link, "type", "") == "application/pdf":
                    pdf_url = link.href
                    break
            if not pdf_url:
                pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"

            candidates.append(
                CandidatePaper(
                    arxiv_id=arxiv_id,
                    title=entry.title.strip().replace("\n", " "),
                    authors=[a.name for a in entry.authors],
                    abstract=entry.summary.strip().replace("\n", " "),
                    categories=[tag.term for tag in entry.tags] if hasattr(entry, "tags") else [],
                    published_at=published,
                    url=entry.id,
                    pdf_url=pdf_url,
                )
            )

        self._save_cached_candidates(candidates)
        return candidates

    def _cache_path(self) -> Path | None:
        raw_dir = getattr(self._settings, "artifacts_dir", None)
        if not raw_dir:
            return None
        return Path(raw_dir) / "arxiv_candidate_cache.json"

    def _save_cached_candidates(self, candidates: list[CandidatePaper]) -> None:
        path = self._cache_path()
        if path is None:
            return
        payload = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "candidates": [
                {
                    "arxiv_id": item.arxiv_id,
                    "title": item.title,
                    "authors": item.authors,
                    "abstract": item.abstract,
                    "categories": item.categories,
                    "published_at": item.published_at.isoformat(),
                    "url": item.url,
                    "pdf_url": item.pdf_url,
                }
                for item in candidates
            ],
        }
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:
            logger.warning("Unable to persist arXiv candidate cache: %s", exc)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _load_cached_candidates(self, hours_back: int) -> list[CandidatePaper]:
        path = self._cache_path()
        if path is None or not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            fetched_at = datetime.fromisoformat(str(payload["fetched_at"]))
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - fetched_at > ARXIV_CACHE_MAX_AGE:
                return []
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
            candidates: list[CandidatePaper] = []
            for item in payload.get("candidates", []):
                published_at = datetime.fromisoformat(str(item["published_at"]))
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=timezone.utc)
                if published_at < cutoff:
                    continue
                candidates.append(
                    CandidatePaper(
                        arxiv_id=str(item["arxiv_id"]),
                        title=str(item["title"]),
                        authors=[str(value) for value in item.get("authors", [])],
                        abstract=str(item["abstract"]),
                        categories=[str(value) for value in item.get("categories", [])],
                        published_at=published_at,
                        url=str(item["url"]),
                        pdf_url=str(item["pdf_url"]),
                    )
                )
            return candidates
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Unable to load arXiv candidate cache: %s", exc)
            return []

    @staticmethod
    def _query_urls(configured_url: str) -> list[str]:
        parsed = urlparse(configured_url)
        if parsed.scheme.lower() == "http" and parsed.hostname == "export.arxiv.org":
            configured_url = urlunparse(parsed._replace(scheme="https"))
            parsed = urlparse(configured_url)

        urls = [configured_url]
        if parsed.hostname == "export.arxiv.org":
            urls.append("https://arxiv.org/api/query")
        return urls

    def _respect_request_interval(self) -> None:
        now = time.monotonic()
        if self._last_request_at is not None:
            remaining = ARXIV_MIN_REQUEST_INTERVAL_SECONDS - (now - self._last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_at = time.monotonic()

    @staticmethod
    def _retry_delay(exc: requests.RequestException, attempt: int) -> int:
        response = getattr(exc, "response", None)
        retry_after = str(response.headers.get("Retry-After", "")).strip() if response is not None else ""
        if retry_after:
            try:
                return min(30, max(1, int(retry_after)))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                    return min(30, max(1, int((retry_at - datetime.now(retry_at.tzinfo)).total_seconds())))
                except (TypeError, ValueError, OverflowError):
                    pass
        return ARXIV_RETRY_DELAYS[min(attempt, len(ARXIV_RETRY_DELAYS) - 1)]

    def rank_and_pick(self, candidates: list[CandidatePaper], top_k: int) -> list[tuple[CandidatePaper, float]]:
        scored = [(paper, self._heuristic_score(paper)) for paper in candidates]

        llm_scores = self._llm_service.score_papers(
            [
                {
                    "arxiv_id": p.arxiv_id,
                    "title": p.title,
                    "abstract": p.abstract,
                    "categories": p.categories,
                }
                for p in candidates
            ]
        )

        final_scores: list[tuple[CandidatePaper, float]] = []
        for paper, heuristic in scored:
            if paper.arxiv_id in llm_scores:
                score = 0.35 * heuristic + 0.65 * llm_scores[paper.arxiv_id]
            else:
                score = heuristic
            final_scores.append((paper, score))

        final_scores.sort(key=lambda item: item[1], reverse=True)
        return final_scores[:top_k]

    def _heuristic_score(self, paper: CandidatePaper) -> float:
        text = f"{paper.title} {paper.abstract}".lower()
        score = 0.0
        for keyword, weight in self._keywords.items():
            if keyword in text:
                score += weight
        if any(cat.startswith("q-fin") for cat in paper.categories):
            score += 10
        return score
