"""Expand source-defined math macros in stored drafts, preserving prose."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.core.config import get_settings
from app.db.models import Paper, PublicationTask, WorkflowRun
from app.db.session import SessionLocal
from app.services.paper_models import ArticleBlockIR, ArticleDocument, ArticlePlan, EvidenceLedger, PaperIR
from app.services.paper_parser import PaperParser
from app.services.paper_pipeline import PaperUnderstandingPipeline
from app.services.llm_service import LLMService
from app.services.workflow_service import WorkflowService


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--refresh-figures", action="store_true")
    args = parser.parse_args()
    settings = get_settings().model_copy(deep=True)
    with SessionLocal() as db:
        WorkflowService(settings)._apply_runtime_settings(db)
        pipeline = PaperUnderstandingPipeline(LLMService(settings), settings)
        for paper in db.query(Paper).filter(Paper.run_id == args.run_id).all():
            directory = Path(settings.artifacts_dir) / "runs" / f"run_{args.run_id}" / "papers" / paper.arxiv_id.replace("/", "_")
            document = ArticleDocument.model_validate_json((directory / "article_document.json").read_text())
            ir = PaperIR.model_validate_json((directory / "paper_ir.json").read_text())
            ledger = EvidenceLedger.model_validate_json((directory / "evidence_ledger.json").read_text())
            macros = PaperParser._read_math_macros("\n".join(text for _, text in PaperParser._read_tex_files(directory / "source")))
            for equation in ir.equations:
                equation.latex = PaperParser._expand_math_macros(equation.latex, macros)
            for section in document.sections:
                for block in section.blocks:
                    if block.type == "equation":
                        block.text = PaperParser._expand_math_macros(block.text, macros)
            if args.refresh_figures:
                for figure in ir.figures:
                    figure.selected = False
                pipeline._link_figures_to_claims(ir, ledger)
                pipeline._select_figures(ir)
                plan = ArticlePlan.model_validate_json((directory / "article_plan.json").read_text())
                pipeline._ensure_figure_placements(plan, ir, ledger)
                claim_lookup = {claim.id: claim for claim in ledger.claims}
                for section in document.sections:
                    section.blocks = [block for block in section.blocks if block.type != "figure"]
                    planned = next((item for item in plan.sections if item.id == section.id), None)
                    if not planned:
                        continue
                    for placement in planned.figure_placements:
                        figure = next((item for item in ir.figures if item.id == placement.figure_id and item.selected), None)
                        if not figure:
                            continue
                        index = next((i + 1 for i, block in enumerate(section.blocks) if placement.after_claim_id in block.claim_ids), None)
                        if index is None:
                            claim = claim_lookup[placement.after_claim_id]
                            section.blocks.append(ArticleBlockIR(id=f"figure-anchor-{figure.id}", text=claim.claim, claim_ids=[claim.id]))
                            index = len(section.blocks)
                        section.blocks.insert(index, ArticleBlockIR(id=f"figure-{figure.id}", type="figure", asset_id=figure.id))
                (directory / "article_plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
            issues = PaperUnderstandingPipeline._deterministic_issues(document, ir, ledger, strict_quality=True)
            if issues:
                raise RuntimeError(f"Deterministic quality check failed: {issues}")
            backup = directory / "note.before_math.md"
            if not backup.exists():
                backup.write_text(paper.note.markdown_content, encoding="utf-8")
            note = PaperUnderstandingPipeline._render_markdown(document, ir)
            paper.note.markdown_content = note
            paper.note.image_paths = json.dumps([figure.artifact_url for figure in ir.figures if figure.selected])
            for task in db.query(PublicationTask).filter(PublicationTask.paper_id == paper.id).all():
                if task.status == "PUBLISHED":
                    raise RuntimeError("Published articles require a separate explicit revision flow")
                task.note_markdown = note
            (directory / "article_document.json").write_text(document.model_dump_json(indent=2), encoding="utf-8")
            (directory / "paper_ir.json").write_text(ir.model_dump_json(indent=2), encoding="utf-8")
            (directory / "note.md").write_text(note, encoding="utf-8")
            report_path = directory / "qa_report.json"
            report = json.loads(report_path.read_text())
            report["metrics"].update(pipeline._quality_metrics(document, ir, ledger))
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            run = db.get(WorkflowRun, args.run_id)
            if run.summary:
                summary = json.loads(run.summary)
                for item in summary.get("article_quality", []):
                    if item.get("arxiv_id") == paper.arxiv_id:
                        item["metrics"] = report["metrics"]
                run.summary = json.dumps(summary, ensure_ascii=False)
            db.commit()
            print(json.dumps({"paper_id": paper.id, "arxiv_id": paper.arxiv_id, "macros": len(macros)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
