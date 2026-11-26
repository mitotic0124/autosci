from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import get_settings
from app.db.models import Paper
from app.db.session import SessionLocal
from app.services.arxiv_service import CandidatePaper
from app.services.llm_service import LLMService
from app.services.paper_pipeline import PaperUnderstandingPipeline
from app.services.workflow_service import WorkflowService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the evidence-grounded article pipeline on one stored paper.")
    parser.add_argument("--arxiv-id", help="Stored arXiv ID. Defaults to the newest paper with a local PDF.")
    parser.add_argument("--output-dir", type=Path, help="Validation output directory.")
    parser.add_argument("--offline", action="store_true", help="Disable all LLM calls and validate extractive fallback.")
    parser.add_argument("--no-source", action="store_true", help="Do not download arXiv TeX source.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings().model_copy(deep=True)
    workflow = WorkflowService(settings)
    db = SessionLocal()
    try:
        workflow._apply_runtime_settings(db)
        query = db.query(Paper)
        if args.arxiv_id:
            query = query.filter(Paper.arxiv_id == args.arxiv_id)
        rows = query.order_by(Paper.id.desc()).all()
        paper = next((item for item in rows if _pdf_path(settings.artifacts_dir, item).exists()), None)
        if paper is None:
            raise RuntimeError("No stored paper with a local PDF was found.")
    finally:
        db.close()

    if args.offline:
        settings.llm_provider = "disabled"
        settings.llm_api_key = None
        settings.openai_api_key = None
    if args.no_source:
        settings.article_fetch_arxiv_source = False

    candidate = CandidatePaper(
        arxiv_id=paper.arxiv_id,
        title=paper.title,
        authors=[value.strip() for value in paper.authors.split(",") if value.strip()],
        abstract=paper.abstract,
        categories=[value.strip() for value in paper.categories.split(",") if value.strip()],
        published_at=paper.published_at or datetime.now(timezone.utc),
        url=paper.url,
        pdf_url=paper.pdf_url,
    )
    output_dir = args.output_dir or Path(settings.artifacts_dir) / "validation" / paper.arxiv_id.replace("/", "_")
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    package = PaperUnderstandingPipeline(LLMService(settings), settings).run(
        candidate,
        pdf_path=_pdf_path(settings.artifacts_dir, paper),
        paper_dir=output_dir,
    )
    result = {
        "arxiv_id": paper.arxiv_id,
        "output_dir": str(output_dir),
        "quality": package.quality_report.model_dump(),
        "selected_image_urls": package.selected_image_urls,
        "artifacts": {key: str(path) for key, path in package.artifact_paths.items()},
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _pdf_path(artifacts_dir: str, paper: Paper) -> Path:
    safe_id = paper.arxiv_id.replace("/", "_")
    return Path(artifacts_dir) / "runs" / f"run_{paper.run_id}" / "papers" / safe_id / f"{safe_id}.pdf"


if __name__ == "__main__":
    raise SystemExit(main())
