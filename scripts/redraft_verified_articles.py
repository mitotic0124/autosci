"""Redraft stored articles from verified evidence, retaining source artifacts and URLs."""
import argparse
import hashlib
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.config import get_settings
from app.db import crud
from app.db.models import PublicationTask
from app.db.session import SessionLocal
from app.services.llm_service import LLMService
from app.services.paper_models import ArticleDocument, ArticlePlan, EvidenceLedger, PaperIR
from app.services.paper_parser import PaperParser
from app.services.paper_pipeline import ArticleQualityError, PaperUnderstandingPipeline
from app.services.workflow_service import WorkflowService


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--codes", nargs="*")
    parser.add_argument("--resume-candidate", action="store_true", help="Reuse a staged draft for the same model and evidence before rerunning verification")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings().model_copy(deep=True)
    with SessionLocal() as db:
        WorkflowService(settings)._apply_runtime_settings(db)
        pipeline = PaperUnderstandingPipeline(LLMService(settings), settings)
        run = crud.get_run(db, args.run_id)
        summary = json.loads(run.summary or "{}")
        tasks = db.query(PublicationTask).filter(PublicationTask.run_id == args.run_id).order_by(PublicationTask.id).all()
        for task in tasks:
            if args.codes and task.paper_code not in args.codes:
                continue
            if task.status == "PUBLISHED":
                raise RuntimeError(f"Refusing to redraft published task {task.paper_code}")
            directory = Path(settings.artifacts_dir) / "runs" / f"run_{args.run_id}" / "papers" / task.paper.arxiv_id.replace("/", "_")
            previous = json.loads((directory / "qa_report.json").read_text(encoding="utf-8"))
            if not previous.get("passed") or previous.get("mode") != "verified":
                raise RuntimeError(f"Unverified evidence for {task.paper_code}")
            paper_ir = PaperIR.model_validate_json((directory / "paper_ir.json").read_text(encoding="utf-8"))
            paper_ir.math_macros = PaperParser._read_math_macros("\n".join(text for _, text in PaperParser._read_tex_files(directory / "source")))
            ledger = EvidenceLedger.model_validate_json((directory / "evidence_ledger.json").read_text(encoding="utf-8"))
            if not ledger.claims or not all(claim.verified for claim in ledger.claims):
                raise RuntimeError(f"Incomplete evidence verification for {task.paper_code}")
            candidate_dir = directory / "redraft-candidate"
            candidate_dir.mkdir(exist_ok=True)
            identity = {"model": settings.llm_article_model, "evidence_sha256": hashlib.sha256(ledger.model_dump_json().encode()).hexdigest()}
            metadata_path = candidate_dir / "metadata.json"
            cached = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
            if args.resume_candidate and cached == identity:
                plan = ArticlePlan.model_validate_json((candidate_dir / "plan.json").read_text())
                document = ArticleDocument.model_validate_json((candidate_dir / "document.json").read_text())
                complete = True
                logging.info("%s: resuming staged draft for verification", task.paper_code)
            else:
                metadata_path.write_text("{}", encoding="utf-8")
                logging.info("%s: planning distinct sections", task.paper_code)
                plan = pipeline._build_article_plan(paper_ir, ledger)
                logging.info("%s: drafting %d sections", task.paper_code, len(plan.sections))
                document, complete = pipeline._draft_document(paper_ir, ledger, plan)
                (candidate_dir / "plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
                (candidate_dir / "document.json").write_text(document.model_dump_json(indent=2), encoding="utf-8")
                metadata_path.write_text(json.dumps(identity if complete else {}), encoding="utf-8")
            logging.info("%s: independent verification", task.paper_code)
            try:
                document, report = pipeline._verify_and_repair(document, paper_ir, ledger, plan, evidence_verified=True, drafting_complete=complete)
            except ArticleQualityError as exc:
                (candidate_dir / "rejected_document.json").write_text(exc.document.model_dump_json(indent=2), encoding="utf-8")
                (candidate_dir / "rejected_report.json").write_text(exc.report.model_dump_json(indent=2), encoding="utf-8")
                raise
            markdown = pipeline._render_markdown(document, paper_ir)
            backup = directory / ("before-redraft-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
            backup.mkdir()
            for filename in ("paper_ir.json", "evidence_ledger.json", "article_plan.json", "article_document.json", "qa_report.json", "note.md"):
                shutil.copy2(directory / filename, backup / filename)
            pipeline._save_artifacts(directory, paper_ir, ledger, plan, document, report, markdown)
            task.note_markdown = markdown
            task.title = document.title
            task.status = "PENDING"
            task.error_message = None
            task.published_at = None
            task.paper.note.markdown_content = markdown
            qualities = summary.setdefault("article_quality", [])
            qualities[:] = [item for item in qualities if item.get("arxiv_id") != task.paper.arxiv_id]
            qualities.append({"arxiv_id": task.paper.arxiv_id, **report.model_dump()})
            run.summary = json.dumps(summary, ensure_ascii=False)
            db.commit()
            print(json.dumps({"code": task.paper_code, "passed": report.passed, "metrics": report.metrics, "backup": str(backup)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
