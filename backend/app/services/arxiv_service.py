from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re

import feedparser
import requests

from app.core.config import Settings
from app.services.llm_service import LLMService


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
        response = requests.get(self._settings.arxiv_base_url, params=params, timeout=30)
        response.raise_for_status()

        feed = feedparser.parse(response.text)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

        candidates: list[CandidatePaper] = []
        for entry in feed.entries:
            published = datetime.strptime(entry.published, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
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

        return candidates

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
