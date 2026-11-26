"""Retry automatic article checkpoints through the production gates; never publish."""
import argparse
import hashlib
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
from app.services.llm_service import LLMService
from app.services.paper_models import ArticleDocument, ArticlePlan, EvidenceLedger, PaperIR, QualityIssueIR, QualityReport
from app.services.paper_pipeline import ArticleQualityError, PaperUnderstandingPipeline
from app.services.publication_service import PublicationService
from app.services.workflow_service import WorkflowService


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--source-run", type=int, required=True)
    parser.add_argument("--arxiv-id", nargs="+", required=True)
    parser.add_argument("--refresh-figures", action="store_true", help="Re-extract figure crops from the original PDF with current boundaries")
    parser.add_argument("--review-findings", type=Path, help="Carry independent findings tied to the exact checkpoint document into verification")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings().model_copy(deep=True)
    with SessionLocal() as db:
        WorkflowService(settings)._apply_runtime_settings(db)
        settings.zhihu_auto_publish = False
        settings.workflow_auto_publish_after_run = False
        settings.article_require_verified = True
        sources = db.query(Paper).filter(Paper.run_id == args.source_run, Paper.arxiv_id.in_(args.arxiv_id)).all()
        if len(sources) != len(set(args.arxiv_id)):
            raise RuntimeError("Source paper metadata is incomplete")
        source_run = crud.get_run(db, args.run_id)
        if source_run is None or source_run.trigger_source not in {"quality_rebuild", "verification_retry", "scheduler", "manual"}:
            raise RuntimeError("Checkpoint must belong to an automatic pipeline run")
        pipeline = PaperUnderstandingPipeline(LLMService(settings), settings)
        run = crud.create_run(db, trigger_source="verification_retry")
        print(f"RUN_ID={run.id}", flush=True)
        papers, notes, reports, errors = [], {}, [], []
        for source in sources:
            safe_id = source.arxiv_id.replace("/", "_")
            old_dir = Path(settings.artifacts_dir) / "runs" / f"run_{args.run_id}" / "papers" / safe_id
            new_dir = Path(settings.artifacts_dir) / "runs" / f"run_{run.id}" / "papers" / safe_id
            new_dir.mkdir(parents=True, exist_ok=True)
            try:
                previous = QualityReport.model_validate_json((old_dir / "qa_report.json").read_text())
                if any(issue.code == "verified_article_required" for issue in previous.issues):
                    raise RuntimeError("Incomplete evidence or drafting requires a fresh generation")
                state_path = old_dir / "pipeline_state.json"
                state = json.loads(state_path.read_text()) if state_path.exists() else {}
                if not state and previous.passed and previous.mode == "verified" and previous.metrics.get("article_context_verified"):
                    state = {"evidence_verified": True, "drafting_complete": True}
                if state.get("evidence_verified") is not True or state.get("drafting_complete") is not True:
                    raise RuntimeError("No complete generation checkpoint; rerun from the original PDF")
                (new_dir / "pipeline_state.json").write_text(json.dumps(state), encoding="utf-8")
                paper_ir = PaperIR.model_validate_json((old_dir / "paper_ir.json").read_text())
                ledger = EvidenceLedger.model_validate_json((old_dir / "evidence_ledger.json").read_text())
                plan = ArticlePlan.model_validate_json((old_dir / "article_plan.json").read_text())
                candidate_path = old_dir / "rejected_document.json"
                if not candidate_path.exists():
                    candidate_path = old_dir / "article_document.json"
                document = ArticleDocument.model_validate_json(candidate_path.read_text())
                if args.review_findings:
                    review = json.loads(args.review_findings.read_text(encoding="utf-8"))
                    identity = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
                    if len(sources) != 1 or review.get("run_id") != args.run_id or review.get("arxiv_id") != source.arxiv_id or review.get("document_sha256") != identity:
                        raise RuntimeError("Independent findings do not match this exact checkpoint")
                    findings = [QualityIssueIR.model_validate(value) for value in review.get("issues", [])]
                    paragraph_ids = {block.id for section in document.sections for block in section.blocks if block.type == "paragraph"}
                    if not findings or any(issue.severity != "error" or issue.block_id not in paragraph_ids or issue.code not in {"unsupported_paragraph", "article_context_mismatch", "article_global_mismatch"} for issue in findings):
                        raise RuntimeError("Independent findings must identify actual paragraph defects")
                    previous.issues.extend(findings)
                    shutil.copy2(args.review_findings, new_dir / "independent_review_findings.json")
                if paper_ir.metadata.arxiv_id != source.arxiv_id or not ledger.claims or not all(c.verified and c.evidence for c in ledger.claims):
                    raise RuntimeError("Checkpoint evidence is incomplete or belongs to a different paper")
                for filename in (f"{safe_id}.pdf", "source", "paper_ir_assets"):
                    path = old_dir / filename
                    if path.is_dir():
                        shutil.copytree(path, new_dir / filename)
                    elif path.exists():
                        shutil.copy2(path, new_dir / filename)
                for figure in paper_ir.figures:
                    relative = Path(figure.image_path).resolve().relative_to(old_dir.resolve())
                    figure.image_path = str(new_dir / relative)
                    figure.artifact_url = pipeline._artifact_url(new_dir / relative)
                if (new_dir / "source").exists():
                    paper_ir.structured_source = pipeline._parser._extract_structured_source(new_dir / "source")
                if args.refresh_figures:
                    from app.services.arxiv_service import CandidatePaper
                    candidate = CandidatePaper(arxiv_id=source.arxiv_id, title=source.title, authors=source.authors.split(", "), abstract=source.abstract, categories=source.categories.split(", "), published_at=source.published_at, url=source.url, pdf_url=source.pdf_url)
                    reparsed = pipeline._parser.parse(candidate, new_dir / f"{safe_id}.pdf", new_dir / "paper_ir_assets", source_dir=new_dir / "source" if (new_dir / "source").exists() else None)
                    figure_lookup = {figure.id: figure for figure in reparsed.figures}
                    for figure in paper_ir.figures:
                        fresh = figure_lookup.get(figure.id)
                        if fresh is None or fresh.source_label != figure.source_label or fresh.page != figure.page:
                            raise RuntimeError("Figure identity changed; regenerate the article from source")
                        figure.width, figure.height = fresh.width, fresh.height
                        figure.image_path = fresh.image_path
                        figure.artifact_url = pipeline._artifact_url(Path(fresh.image_path))
                        figure.analysis.caption_verified = False
                for name, value in (("paper_ir", paper_ir), ("evidence_ledger", ledger), ("article_plan", plan), ("article_document", document)):
                    pipeline._write_json(new_dir / f"{name}.json", value)
                logging.info("%s: retrying strict verification of automatic output", safe_id)
                document, report = pipeline._verify_and_repair(document, paper_ir, ledger, plan, evidence_verified=True, drafting_complete=True, previous_issues=previous.issues, checkpoint_dir=new_dir)
                markdown = pipeline._render_markdown(document, paper_ir)
                pipeline._save_artifacts(new_dir, paper_ir, ledger, plan, document, report, markdown)
                paper = Paper(run_id=run.id, arxiv_id=source.arxiv_id, title=source.title, authors=source.authors, abstract=source.abstract, categories=source.categories, published_at=source.published_at, url=source.url, pdf_url=source.pdf_url, score=source.score)
                db.add(paper)
                db.flush()
                used_images = {b.asset_id for s in document.sections for b in s.blocks if b.type == "figure"}
                db.add(PaperNote(paper_id=paper.id, markdown_content=markdown, image_paths=json.dumps([f.artifact_url for f in paper_ir.figures if f.id in used_images])))
                db.commit()
                papers.append(paper)
                notes[paper.id] = markdown
                reports.append({"arxiv_id": source.arxiv_id, **report.model_dump()})
            except Exception as exc:
                if isinstance(exc, ArticleQualityError):
                    pipeline._write_json(new_dir / "rejected_document.json", exc.document)
                    pipeline._write_json(new_dir / "qa_report.json", exc.report)
                logging.exception("Verification retry failed for %s", safe_id)
                errors.append({"arxiv_id": source.arxiv_id, "error": str(exc)})
        tasks = PublicationService(settings).create_tasks_for_run(db, run.id, papers, notes)
        summary = {"source_run": args.source_run, "checkpoint_run": args.run_id, "selected_papers": len(papers), "publication_tasks": len(tasks), "article_quality": reports, "paper_errors": errors, "auto_publish_after_run": {"enabled": False}}
        crud.finish_run(db, run.id, status="FAILED" if errors else "SUCCESS", summary=summary, error_message="Verification retry failed" if errors else None)
        print(json.dumps({"run_id": run.id, "codes": [task.paper_code for task in tasks], "summary": summary}, ensure_ascii=False), flush=True)
        return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
