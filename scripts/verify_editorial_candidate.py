"""Verify a reviewed article against stored source evidence before updating a draft."""
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
from app.db.session import SessionLocal
from app.services.llm_service import LLMService
from app.services.paper_models import ArticleBlockIR, ArticleDocument, ArticlePlan, ArticleSectionIR, ArticleSectionPlanIR, ClaimEvidenceIR, EvidenceClaimIR, EvidenceLedger, PaperIR
from app.services.paper_pipeline import ArticleQualityError, PaperUnderstandingPipeline
from app.services.workflow_service import WorkflowService


def build_candidate(spec, paper):
    source = {block.id: block for block in paper.blocks}
    equations = {equation.id: equation for equation in paper.equations}
    figures = {figure.id: figure for figure in paper.figures}
    for figure in paper.figures:
        figure.selected = False
    document = ArticleDocument(title=spec["title"], arxiv_url=paper.metadata.url, authors=paper.metadata.authors)
    ledger = EvidenceLedger()
    plan = ArticlePlan(title=spec["title"])
    for si, section in enumerate(spec["sections"], 1):
        rendered = ArticleSectionIR(id=f"section-{si}", title=section["title"])
        planned = ArticleSectionPlanIR(id=rendered.id, title=rendered.title, purpose=rendered.title)
        for pi, paragraph in enumerate(section["paragraphs"], 1):
            identity = f"paragraph-{si}-{pi}"
            evidence = []
            for block_id in paragraph["sources"]:
                if block_id in source:
                    block = source[block_id]
                    evidence.append(ClaimEvidenceIR(block_id=block.id, page=block.page, quote=block.text))
                elif block_id in equations:
                    equation = equations[block_id]
                    evidence.append(ClaimEvidenceIR(block_id=equation.id, page=1, quote=equation.context + "\n" + equation.latex))
                else:
                    raise ValueError(f"Unknown source {block_id}")
            if not evidence:
                raise ValueError(f"Missing evidence for {identity}")
            ledger.claims.append(EvidenceClaimIR(id=identity, claim=paragraph["text"], claim_type="method", evidence=evidence))
            rendered.blocks.append(ArticleBlockIR(id=identity, text=paragraph["text"], claim_ids=[identity]))
            planned.claim_ids.append(identity)
            for equation_id in paragraph.get("equations_after", []):
                equation = equations[equation_id]
                rendered.blocks.append(ArticleBlockIR(id=f"{identity}-{equation_id}", type="equation", text=equation.latex, asset_id=equation.id))
                planned.equation_ids.append(equation_id)
            if paragraph.get("figure_after"):
                figure = figures[paragraph["figure_after"]]
                if not figure.analysis.vision_verified:
                    raise ValueError("Figure has not passed visual verification")
                figure.selected = True
                figure.related_claim_ids = [identity]
                if paragraph.get("caption"):
                    figure.analysis.summary = paragraph["caption"]
                rendered.blocks.append(ArticleBlockIR(id=f"{identity}-{figure.id}", type="figure", asset_id=figure.id))
        document.sections.append(rendered)
        plan.sections.append(planned)
    return document, ledger, plan


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings().model_copy(deep=True)
    with SessionLocal() as db:
        WorkflowService(settings)._apply_runtime_settings(db)
        task = crud.get_publication_task_by_code(db, args.code)
        if not task or task.status == "PUBLISHED":
            raise ValueError("Only an unpublished existing task may be revised")
        directory = Path(settings.artifacts_dir) / "runs" / f"run_{task.run_id}" / "papers" / task.paper.arxiv_id.replace("/", "_")
        paper = PaperIR.model_validate_json((directory / "paper_ir.json").read_text(encoding="utf-8"))
        spec = json.loads(args.candidate.read_text(encoding="utf-8"))
        document, ledger, plan = build_candidate(spec, paper)
        pipeline = PaperUnderstandingPipeline(LLMService(settings), settings)
        staged = directory / "editorial-candidate"
        staged.mkdir(exist_ok=True)
        identity = hashlib.sha256((document.model_dump_json() + ledger.model_dump_json() + str(settings.llm_verifier_model)).encode()).hexdigest()
        cache = staged / "evidence_verification.json"
        cached = json.loads(cache.read_text()) if cache.exists() else {}
        if cached.get("identity") != identity or not cached.get("passed"):
            verdicts = pipeline._verify_claims(ledger.claims)
            failures = [c.id for c in ledger.claims if not verdicts or not verdicts.get(c.id, (False, 0))[0]]
            cache.write_text(json.dumps({"identity": identity, "passed": not failures, "failures": failures}), encoding="utf-8")
            if failures:
                raise RuntimeError(f"Source verification failed: {failures}")
        for claim in ledger.claims:
            claim.verified = True
            claim.confidence = 1.0
        try:
            document, report = pipeline._verify_and_repair(document, paper, ledger, plan, evidence_verified=True, drafting_complete=True)
        except ArticleQualityError as exc:
            (staged / "rejected_document.json").write_text(exc.document.model_dump_json(indent=2), encoding="utf-8")
            (staged / "rejected_report.json").write_text(exc.report.model_dump_json(indent=2), encoding="utf-8")
            raise
        markdown = pipeline._render_markdown(document, paper)
        backup = directory / ("before-editorial-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        backup.mkdir()
        for name in ("paper_ir.json", "evidence_ledger.json", "article_plan.json", "article_document.json", "qa_report.json", "note.md"):
            shutil.copy2(directory / name, backup / name)
        pipeline._save_artifacts(directory, paper, ledger, plan, document, report, markdown)
        task.title = document.title
        task.note_markdown = markdown
        task.paper.note.markdown_content = markdown
        task.status = "PENDING"
        task.error_message = None
        task.published_at = None
        run = crud.get_run(db, task.run_id)
        summary = json.loads(run.summary or "{}")
        qualities = [item for item in summary.get("article_quality", []) if item.get("arxiv_id") != task.paper.arxiv_id]
        qualities.append({"arxiv_id": task.paper.arxiv_id, **report.model_dump()})
        summary["article_quality"] = qualities
        run.summary = json.dumps(summary, ensure_ascii=False)
        db.commit()
        print(json.dumps({"code": args.code, "passed": report.passed, "metrics": report.metrics}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
