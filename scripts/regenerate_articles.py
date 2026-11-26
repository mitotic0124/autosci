"""Rebuild a stored run with current quality gates, without publishing or email."""
import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.core.config import get_settings
from app.db import crud
from app.db.models import Paper, PaperNote
from app.db.session import SessionLocal
from app.services.arxiv_service import CandidatePaper
from app.services.llm_service import LLMService
from app.services.paper_pipeline import PaperUnderstandingPipeline
from app.services.publication_service import PublicationService
from app.services.workflow_service import WorkflowService


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-run", type=int, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings().model_copy(deep=True)
    with SessionLocal() as db:
        WorkflowService(settings)._apply_runtime_settings(db)
        settings.zhihu_auto_publish = False
        settings.workflow_auto_publish_after_run = False
        settings.article_require_verified = True
        sources = db.query(Paper).filter(Paper.run_id == args.source_run).order_by(Paper.id).all()
        if not sources:
            raise RuntimeError("No source papers found")
        run = crud.create_run(db, trigger_source="quality_rebuild")
        print(f"RUN_ID={run.id}", flush=True)
        pipeline = PaperUnderstandingPipeline(LLMService(settings), settings)
        papers, notes, reports, errors = [], {}, [], []
        for source in sources:
            safe_id = source.arxiv_id.replace("/", "_")
            old_dir = Path(settings.artifacts_dir) / "runs" / f"run_{args.source_run}" / "papers" / safe_id
            new_dir = Path(settings.artifacts_dir) / "runs" / f"run_{run.id}" / "papers" / safe_id
            new_dir.mkdir(parents=True, exist_ok=True)
            old_pdf = old_dir / f"{safe_id}.pdf"
            pdf_path = new_dir / old_pdf.name
            if old_pdf.exists():
                shutil.copy2(old_pdf, pdf_path)
            if (old_dir / "source").is_dir():
                shutil.copytree(old_dir / "source", new_dir / "source", dirs_exist_ok=True)
            candidate = CandidatePaper(arxiv_id=source.arxiv_id, title=source.title, authors=source.authors.split(", "), abstract=source.abstract, categories=source.categories.split(", "), published_at=source.published_at, url=source.url, pdf_url=source.pdf_url)
            try:
                package = pipeline.run(candidate, pdf_path, new_dir, progress_callback=lambda value: logging.info("%s: %s", safe_id, value))
            except Exception as exc:
                logging.exception("Rebuild failed for %s", safe_id)
                errors.append({"arxiv_id": source.arxiv_id, "error": str(exc)})
                continue
            paper = Paper(run_id=run.id, arxiv_id=source.arxiv_id, title=source.title, authors=source.authors, abstract=source.abstract, categories=source.categories, published_at=source.published_at, url=source.url, pdf_url=source.pdf_url, score=source.score)
            db.add(paper)
            db.flush()
            db.add(PaperNote(paper_id=paper.id, markdown_content=package.markdown, image_paths=json.dumps(package.selected_image_urls)))
            db.commit()
            papers.append(paper)
            notes[paper.id] = package.markdown
            reports.append({"arxiv_id": source.arxiv_id, **package.quality_report.model_dump()})
        tasks = PublicationService(settings).create_tasks_for_run(db, run.id, papers, notes)
        summary = {"source_run": args.source_run, "selected_papers": len(papers), "publication_tasks": len(tasks), "article_quality": reports, "paper_errors": errors, "auto_publish_after_run": {"enabled": False}}
        crud.finish_run(db, run.id, status="SUCCESS" if len(papers) == len(sources) else "FAILED", summary=summary, error_message="Some articles failed quality checks" if errors else None)
        print(json.dumps({"run_id": run.id, "summary": summary}, ensure_ascii=False), flush=True)
        return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
