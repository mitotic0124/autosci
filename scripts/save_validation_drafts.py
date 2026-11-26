"""Save independently verified papers as drafts, recording URLs for task reuse."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.core.config import get_settings
from app.db.models import Paper, PublicationTask
from app.db.session import SessionLocal
from app.services.publication_service import PublicationService
from app.services.workflow_service import WorkflowService
from app.services.zhihu_service import ZhihuService


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--replace-content", action="store_true", help="Replace the body of the retained draft after redrafting")
    args = parser.parse_args()
    settings = get_settings().model_copy(deep=True)
    with SessionLocal() as db:
        WorkflowService(settings)._apply_runtime_settings(db)
        settings.zhihu_auto_publish = False
        service = ZhihuService(settings)
        for paper in db.query(Paper).filter(Paper.run_id == args.run_id).order_by(Paper.id).all():
            directory = Path(settings.artifacts_dir) / "runs" / f"run_{args.run_id}" / "papers" / paper.arxiv_id.replace("/", "_")
            report = json.loads((directory / "qa_report.json").read_text())
            if not report.get("passed") or report.get("mode") != "verified" or not all(report.get("metrics", {}).get(key) for key in ("article_context_verified", "article_global_verified")):
                raise RuntimeError("Unverified article cannot be saved")
            if report.get("metrics", {}).get("figure_count", 0) and not report["metrics"].get("figure_captions_verified"):
                raise RuntimeError("Unverified figure captions cannot be saved")
            record = directory / "draft_acceptance.json"
            old = json.loads(record.read_text()) if record.exists() else {}
            title = PublicationService._pick_publish_title(paper.note.markdown_content, paper.title)
            task = db.query(PublicationTask).filter(PublicationTask.paper_id == paper.id).first()
            try:
                url = service.publish_article(title, paper.note.markdown_content, draft_url=old.get("url") or (task.zhihu_url if task else None), replace_content=args.replace_content)
            except Exception as exc:
                retained_url = getattr(exc, "draft_url", None)
                if retained_url:
                    record.write_text(json.dumps({"paper_id": paper.id, "url": retained_url, "error": str(exc)}, ensure_ascii=False, indent=2), encoding="utf-8")
                if task:
                    task.zhihu_url = retained_url or task.zhihu_url
                    task.status = "FAILED"
                    task.error_message = str(exc)
                    db.commit()
                raise
            record.write_text(json.dumps({"paper_id": paper.id, "url": url}, ensure_ascii=False, indent=2), encoding="utf-8")
            if task:
                task.zhihu_url = url
                task.status = "DRAFT"
                task.error_message = None
                db.commit()
            print(json.dumps({"paper_id": paper.id, "url": url}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
