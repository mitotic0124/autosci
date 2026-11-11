from __future__ import annotations

import json
import logging
import math
import re
import shutil
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterable

from app.core.config import Settings
from app.services.arxiv_service import CandidatePaper
from app.services.llm_service import LLMProviderUnavailable, LLMService
from app.services.math_validation import escape_currency_dollars, malformed_math_reason
from app.services.paper_models import (
    ArticleBlockIR,
    ArticleDocument,
    ArticlePackage,
    ArticlePlan,
    ArticleSectionIR,
    ArticleSectionPlanIR,
    ClaimEvidenceIR,
    EvidenceClaimIR,
    EvidenceLedger,
    FigureAnalysisIR,
    FigureIR,
    FigurePlacementIR,
    PaperIR,
    QualityIssueIR,
    QualityReport,
    TextBlockIR,
)
from app.services.paper_parser import PaperParser

logger = logging.getLogger(__name__)


class ArticleQualityError(RuntimeError):
    def __init__(self, document: ArticleDocument, report: QualityReport) -> None:
        self.document = document
        self.report = report
        super().__init__("Article quality gate failed: " + "; ".join(
            issue.message for issue in report.issues if issue.severity == "error"
        )[:3000])


VALID_CLAIM_TYPES = {
    "research_question",
    "contribution",
    "method",
    "data",
    "assumption",
    "empirical_result",
    "numeric_result",
    "limitation",
    "future_work",
    "conclusion",
}


class PaperUnderstandingPipeline:
    def __init__(self, llm_service: LLMService, settings: Settings, parser: PaperParser | None = None) -> None:
        self._llm = llm_service
        self._settings = settings
        self._parser = parser or PaperParser()
        self._article_model = str(getattr(settings, "llm_article_model", "") or "").strip() or None

    def run(
        self,
        paper: CandidatePaper,
        pdf_path: Path,
        paper_dir: Path,
        progress_callback: Callable[[str], None] | None = None,
    ) -> ArticlePackage:
        try:
            return self._run_pipeline(paper, pdf_path, paper_dir, progress_callback)
        except LLMProviderUnavailable as exc:
            self._record_provider_unavailable(paper_dir, exc)
            raise

    @staticmethod
    def _record_provider_unavailable(paper_dir: Path, exc: Exception) -> None:
        """Persist a provider outage without fabricating a quality rejection.

        ``backfill_quality_revisions`` consumes only failed ``qa_report.json``
        files, so a quota/rate outage must not write one; otherwise a transient
        provider window would be turned into a permanent rework item.
        """
        try:
            paper_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "error": str(exc),
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
            (paper_dir / "provider_error.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            state_path = paper_dir / "pipeline_state.json"
            state: dict[str, Any] = {}
            if state_path.exists():
                try:
                    parsed = json.loads(state_path.read_text(encoding="utf-8"))
                    state = parsed if isinstance(parsed, dict) else {}
                except (OSError, ValueError):
                    state = {}
            state["provider_unavailable"] = str(exc)
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except OSError:
            logger.exception("Unable to record provider outage for %s", paper_dir)

    def _run_pipeline(
        self,
        paper: CandidatePaper,
        pdf_path: Path,
        paper_dir: Path,
        progress_callback: Callable[[str], None] | None = None,
    ) -> ArticlePackage:
        pipeline_started = time.perf_counter()
        timings: dict[str, float] = {}
        paper_dir.mkdir(parents=True, exist_ok=True)
        self._notify(progress_callback, "Downloading and parsing the paper structure.")
        stage_started = time.perf_counter()
        if not pdf_path.exists():
            self._parser.download_pdf(paper.pdf_url, pdf_path)

        source_dir = None
        if bool(getattr(self._settings, "article_fetch_arxiv_source", True)):
            source_dir = self._parser.download_source(paper.arxiv_id, paper_dir / "source")

        paper_ir = self._parser.parse(paper, pdf_path, paper_dir / "paper_ir_assets", source_dir=source_dir)
        for figure in paper_ir.figures:
            figure.artifact_url = self._artifact_url(Path(figure.image_path))
        timings["parse"] = time.perf_counter() - stage_started
        self._write_json(paper_dir / "paper_ir.json", paper_ir)

        self._notify(progress_callback, "Analyzing captioned figures with the configured vision model.")
        stage_started = time.perf_counter()
        self._analyze_figures(paper_ir)
        timings["figure_analysis"] = time.perf_counter() - stage_started
        self._write_json(paper_dir / "paper_ir.json", paper_ir)

        self._notify(progress_callback, "Extracting and independently verifying evidence claims.")
        stage_started = time.perf_counter()
        ledger, evidence_verified = self._build_evidence_ledger(paper_ir)
        timings["evidence"] = time.perf_counter() - stage_started
        self._write_json(paper_dir / "evidence_ledger.json", ledger)

        self._notify(progress_callback, "Linking verified claims to figures and planning the article.")
        stage_started = time.perf_counter()
        self._link_figures_to_claims(paper_ir, ledger)
        self._select_figures(paper_ir)
        plan = self._build_article_plan(paper_ir, ledger)
        timings["planning"] = time.perf_counter() - stage_started
        self._write_json(paper_dir / "paper_ir.json", paper_ir)
        self._write_json(paper_dir / "article_plan.json", plan)

        self._notify(progress_callback, "Drafting sections from their allowed evidence packets.")
        stage_started = time.perf_counter()
        document, drafting_complete = self._draft_document(paper_ir, ledger, plan)
        timings["drafting"] = time.perf_counter() - stage_started
        self._write_json(paper_dir / "article_document.json", document)
        (paper_dir / "pipeline_state.json").write_text(json.dumps({
            "evidence_verified": evidence_verified,
            "drafting_complete": drafting_complete,
            "article_model": self._article_model,
        }), encoding="utf-8")

        self._notify(progress_callback, "Running article-level quality gates and targeted repair.")
        stage_started = time.perf_counter()
        try:
            document, report = self._verify_and_repair(
                document, paper_ir, ledger, plan,
                evidence_verified=evidence_verified, drafting_complete=drafting_complete,
                checkpoint_dir=paper_dir,
            )
        except ArticleQualityError as exc:
            self._write_json(paper_dir / "rejected_document.json", exc.document)
            self._write_json(paper_dir / "qa_report.json", exc.report)
            raise
        except LLMProviderUnavailable as exc:
            self._write_json(paper_dir / "rejected_document.json", document)
            report_path = paper_dir / "qa_report.json"
            previous_issues = []
            if report_path.exists():
                previous_issues = QualityReport.model_validate_json(report_path.read_text(encoding="utf-8")).issues
            self._write_json(report_path, QualityReport(
                passed=False, mode="failed", score=0.0,
                metrics={**self._quality_metrics(document, paper_ir, ledger), "article_context_verified": False, "article_global_verified": False},
                issues=[*previous_issues, QualityIssueIR(code="llm_provider_unavailable", message=str(exc))],
            ))
            raise
        timings["verification"] = time.perf_counter() - stage_started
        timings["total"] = time.perf_counter() - pipeline_started
        report.metrics.update({f"stage_seconds_{key}": round(value, 3) for key, value in timings.items()})
        markdown = self._render_markdown(document, paper_ir)

        artifact_paths = self._save_artifacts(paper_dir, paper_ir, ledger, plan, document, report, markdown)
        selected_ids = {
            block.asset_id
            for section in document.sections
            for block in section.blocks
            if block.type == "figure" and block.asset_id
        }
        selected_urls = [figure.artifact_url for figure in paper_ir.figures if figure.id in selected_ids]
        return ArticlePackage(
            document=document,
            markdown=markdown,
            selected_image_urls=selected_urls,
            quality_report=report,
            artifact_paths=artifact_paths,
        )

    @staticmethod
    def _notify(callback: Callable[[str], None] | None, message: str) -> None:
        if callback:
            callback(message)

    def revise_from_checkpoint(
        self, paper: CandidatePaper, pdf_path: Path, paper_dir: Path,
        progress_callback: Callable[[str], None] | None = None,
        *, force_redraft: bool = False,
    ) -> ArticlePackage:
        """Continue a rejected article using its evidence and recorded findings."""
        names = ("paper_ir.json", "evidence_ledger.json", "article_plan.json",
                 "article_document.json", "rejected_document.json", "qa_report.json", "pipeline_state.json")
        archive_root = paper_dir / "revisions"
        archive = archive_root / f"attempt_{len(list(archive_root.glob('attempt_*'))) + 1:03d}"
        archive.mkdir(parents=True, exist_ok=False)
        for name in names:
            if (paper_dir / name).exists():
                shutil.copy2(paper_dir / name, archive / name)
        required = ("paper_ir.json", "evidence_ledger.json", "article_plan.json", "pipeline_state.json")
        if not all((paper_dir / name).exists() for name in required):
            return self.run(paper, pdf_path, paper_dir, progress_callback)
        paper_ir = PaperIR.model_validate_json((paper_dir / "paper_ir.json").read_text(encoding="utf-8"))
        ledger = EvidenceLedger.model_validate_json((paper_dir / "evidence_ledger.json").read_text(encoding="utf-8"))
        state = json.loads((paper_dir / "pipeline_state.json").read_text(encoding="utf-8"))
        if paper_ir.metadata.arxiv_id != paper.arxiv_id:
            raise RuntimeError("Revision checkpoint belongs to a different paper")
        if state.get("evidence_verified") is not True or not ledger.claims or not all(c.verified and c.evidence for c in ledger.claims):
            return self.run(paper, pdf_path, paper_dir, progress_callback)
        previous = QualityReport.model_validate_json((paper_dir / "qa_report.json").read_text(encoding="utf-8"))
        plan = ArticlePlan.model_validate_json((paper_dir / "article_plan.json").read_text(encoding="utf-8"))
        path = paper_dir / "rejected_document.json"
        if not path.exists():
            path = paper_dir / "article_document.json"
        document = ArticleDocument.model_validate_json(path.read_text(encoding="utf-8"))
        structural = {"article_too_short", "article_section_coverage", "non_chinese_title",
                      "unsupported_future_section", "missing_provenance", "duplicate_equation",
                      "invalid_equation", "figure_placement_mismatch", "duplicate_figure"}
        redraft = force_redraft or state.get("drafting_complete") is not True or any(
            issue.code in structural for issue in previous.issues
        )
        previous_issues = previous.issues
        if redraft:
            self._notify(progress_callback, "Replanning and rewriting the rejected article from verified evidence.")
            plan = self._build_article_plan(paper_ir, ledger)
            document, complete = self._draft_document(paper_ir, ledger, plan, revision_issues=previous.issues)
            state["drafting_complete"] = complete
            # IDs in a new plan may be reused for different paragraphs.
            previous_issues = []
            self._write_json(paper_dir / "article_plan.json", plan)
            self._write_json(paper_dir / "article_document.json", document)
            self._write_json(paper_dir / "rejected_document.json", document)
            (paper_dir / "pipeline_state.json").write_text(json.dumps(state), encoding="utf-8")
        self._notify(progress_callback, "Revising recorded findings and independently reviewing the final article.")
        try:
            document, report = self._verify_and_repair(
                document, paper_ir, ledger, plan, evidence_verified=True,
                drafting_complete=state.get("drafting_complete") is True,
                previous_issues=previous_issues, checkpoint_dir=paper_dir,
            )
        except ArticleQualityError as exc:
            self._write_json(paper_dir / "rejected_document.json", exc.document)
            self._write_json(paper_dir / "qa_report.json", exc.report)
            raise
        markdown = self._render_markdown(document, paper_ir)
        paths = self._save_artifacts(paper_dir, paper_ir, ledger, plan, document, report, markdown)
        used = {b.asset_id for s in document.sections for b in s.blocks if b.type == "figure"}
        return ArticlePackage(document=document, markdown=markdown,
            selected_image_urls=[f.artifact_url for f in paper_ir.figures if f.id in used],
            quality_report=report, artifact_paths=paths)

    @staticmethod
    def _write_json(path: Path, model: Any) -> None:
        path.write_text(model.model_dump_json(indent=2), encoding="utf-8")

    def _analyze_figures(self, paper_ir: PaperIR) -> None:
        block_lookup = {block.id: block for block in paper_ir.blocks}
        max_candidates = max(1, int(getattr(self._settings, "article_max_figure_candidates", 12)))
        vision_model = str(getattr(self._settings, "llm_vision_model", "") or "").strip() or None
        candidates = sorted(
            paper_ir.figures,
            key=lambda figure: (figure.reference_count, figure.width * figure.height),
            reverse=True,
        )[:max_candidates]
        if not vision_model:
            if candidates:
                paper_ir.parse_warnings.append(
                    "No llm_vision_model is configured; figures remain unverified and are excluded from verified articles."
                )
            for figure in candidates:
                figure.analysis = self._figure_analysis_from_payload(None, figure)
            return

        def analyze(figure: FigureIR) -> tuple[FigureIR, dict[str, Any] | None]:
            context = [block_lookup[block_id].text for block_id in figure.context_block_ids if block_id in block_lookup]
            payload = None
            if self._llm.enabled:
                payload = self._llm.generate_vision_json(
                    system_prompt=(
                        "你是科研论文图表审阅员。只依据图片像素、原始图注和相邻原文判断，"
                        "不得推测图片中不可见或原文没有说明的结果。只返回 JSON 对象。"
                    ),
                    user_prompt=(
                        "分析这张论文图，并返回："
                        '{"visual_type":"", "summary":"", "key_findings":[], '
                        '"legibility":0.0, "importance":0.0, "decorative":false, "confidence":0.0}。'
                        "所有分数范围为 0 到 1。summary 使用中文，但不能超出图注与图片能够支持的内容。\n"
                        f"原始标号：{figure.source_label}\n"
                        f"PDF 页码：{figure.page}\n"
                        f"原始图注：{figure.caption}\n"
                        f"相邻原文：{json.dumps(context, ensure_ascii=False)}"
                    ),
                    image_path=Path(figure.image_path),
                    model=vision_model,
                )
            return figure, payload

        workers = self._parallel_workers(len(candidates))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="autosci-vision") as executor:
            results = list(executor.map(analyze, candidates))
        for figure, payload in results:
            figure.analysis = self._figure_analysis_from_payload(payload, figure)
        if self._settings.article_require_verified:
            self._verify_figure_captions(candidates)

    def _verify_figure_captions(self, figures: list[FigureIR]) -> None:
        candidates = [figure for figure in figures if figure.analysis.vision_verified]
        if not candidates:
            return
        response = self._llm.generate_json(
            system_prompt=("你是论文图注翻译员。仅将原始图注准确译为中文，不使用视觉摘要补充内容。"
                "不得推测图注未提及的横纵轴、趋势、曲线、收益变化或因果关系。保留原文条件与数字。"
                "删除 label 等排版命令；变量优先用中文描述，必要的数学表达式必须用 $...$ 包裹。只返回 JSON。"),
            user_prompt='返回 {"captions":[{"figure_id":"fig-001","text":"中文图注"}]}。\n' + json.dumps(
                [{"figure_id": f.id, "caption": f.caption} for f in candidates], ensure_ascii=False),
            model=self._article_model,
        )
        rows = response.get("captions", []) if isinstance(response, dict) else []
        if not isinstance(rows, list):
            rows = []
        translated = {str(row.get("figure_id")): str(row.get("text", "")).strip()
                      for row in rows if isinstance(row, dict)}
        claims = [EvidenceClaimIR(id=f.id, claim=translated[f.id], claim_type="data",
            evidence=[ClaimEvidenceIR(block_id=f.id, page=f.page, quote=f.caption)])
            for f in candidates if translated.get(f.id) and _cjk_ratio(translated[f.id]) >= 0.15]
        verdicts = self._verify_claims(claims) if claims else None
        for figure in candidates:
            figure.analysis.caption_verified = bool(verdicts and verdicts.get(figure.id, (False, 0.0))[0])
            if figure.analysis.caption_verified:
                figure.analysis.summary = translated[figure.id]

    def _parallel_workers(self, item_count: int) -> int:
        configured = max(1, int(getattr(self._settings, "article_parallel_workers", 3)))
        return max(1, min(configured, max(1, item_count)))

    @staticmethod
    def _figure_analysis_from_payload(payload: dict[str, Any] | None, figure: FigureIR) -> FigureAnalysisIR:
        if not payload:
            caption = figure.caption.strip()
            decorative = bool(re.search(r"logo|author photo|graphical abstract", caption, re.I))
            return FigureAnalysisIR(
                visual_type="unknown",
                summary=caption,
                key_findings=[],
                legibility=0.65 if figure.width >= 700 else 0.5,
                importance=0.6 if figure.reference_count else 0.45,
                decorative=decorative,
                confidence=0.35,
                vision_verified=False,
            )
        findings = payload.get("key_findings")
        if not isinstance(findings, list):
            findings = []
        return FigureAnalysisIR(
            visual_type=str(payload.get("visual_type") or "unknown")[:80],
            summary=str(payload.get("summary") or figure.caption).strip()[:1200],
            key_findings=[str(item).strip()[:500] for item in findings if str(item).strip()][:6],
            legibility=_clamp_float(payload.get("legibility"), 0.5),
            importance=_clamp_float(payload.get("importance"), 0.5),
            decorative=bool(payload.get("decorative", False)),
            confidence=_clamp_float(payload.get("confidence"), 0.5),
            vision_verified=True,
        )

    def _build_evidence_ledger(self, paper_ir: PaperIR) -> tuple[EvidenceLedger, bool]:
        source_limit = max(12_000, int(getattr(self._settings, "article_max_source_chars", 70_000)))
        source_blocks = _balanced_source_blocks(paper_ir, source_limit)

        if not self._llm.enabled:
            claims = self._extractive_claims(paper_ir, source_blocks)
            return EvidenceLedger(claims=claims, warnings=["LLM unavailable; used extractive evidence mode."]), False

        raw_claims: list[EvidenceClaimIR] = []
        seen: set[str] = set()
        claim_index = 0
        chunks = list(_chunk_blocks(source_blocks, max_chars=9_000, max_items=22))

        def extract(chunk: list[TextBlockIR]) -> dict[str, Any] | None:
            block_payload = [
                {"id": block.id, "page": block.page, "section_id": block.section_id, "text": block.text}
                for block in chunk
            ]
            return self._llm.generate_json(
                system_prompt=(
                    "你是论文证据抽取器。只抽取输入原文明示的事实，不写文章，不补充常识。"
                    "每条声明必须绑定输入中的 evidence_block_ids。只返回 JSON。"
                    "保留结果的归属和适用条件：引用文献或前期研究的结论必须标明既有研究，不得抽成本文贡献或本文结论。"
                ),
                model=self._article_model,
                user_prompt=(
                    "从以下论文段落抽取最多 10 条重要声明。返回结构："
                    '{"claims":[{"claim":"中文准确转述", "claim_type":"method", '
                    '"evidence_block_ids":["..."], "author_asserted":true, "confidence":0.0}]}。'
                    f"claim_type 只能是 {sorted(VALID_CLAIM_TYPES)}。"
                    "只有原文明确提出未来工作时才可使用 future_work。\n"
                    f"段落：{json.dumps(block_payload, ensure_ascii=False)}"
                ),
            )

        workers = self._parallel_workers(len(chunks))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="autosci-evidence") as executor:
            responses = list(executor.map(extract, chunks))

        for chunk, response in zip(chunks, responses):
            if not response or not isinstance(response.get("claims"), list):
                continue
            lookup = {block.id: block for block in chunk}
            for item in response["claims"][:12]:
                if not isinstance(item, dict):
                    continue
                text = re.sub(r"\s+", " ", str(item.get("claim") or "")).strip()
                claim_type = str(item.get("claim_type") or "conclusion")
                if not text or claim_type not in VALID_CLAIM_TYPES:
                    continue
                normalized = _normalize_text(text)
                if normalized in seen:
                    continue
                evidence_ids = item.get("evidence_block_ids")
                if not isinstance(evidence_ids, list):
                    continue
                evidence = [
                    ClaimEvidenceIR(block_id=block_id, page=lookup[block_id].page, quote=lookup[block_id].text)
                    for block_id in dict.fromkeys(str(value) for value in evidence_ids)
                    if block_id in lookup
                ]
                if not evidence:
                    continue
                claim_index += 1
                seen.add(normalized)
                raw_claims.append(
                    EvidenceClaimIR(
                        id=f"claim-{claim_index:03d}",
                        claim=text[:1200],
                        claim_type=claim_type,
                        evidence=evidence,
                        author_asserted=bool(item.get("author_asserted", True)),
                        confidence=_clamp_float(item.get("confidence"), 0.6),
                    )
                )

        if not raw_claims:
            claims = self._fallback_claims(paper_ir, source_blocks)
            return EvidenceLedger(
                claims=claims,
                warnings=["Evidence extraction returned no valid JSON; used translated extractive mode."],
            ), False

        raw_claims = _limit_claims(raw_claims, max(12, int(getattr(self._settings, "article_max_claims", 42))))
        verified = self._verify_claims(raw_claims)
        if verified is None:
            claims = self._fallback_claims(paper_ir, source_blocks)
            return EvidenceLedger(
                claims=claims,
                warnings=["Claim verification failed; used translated extractive mode."],
            ), False

        supported: list[EvidenceClaimIR] = []
        for claim in raw_claims:
            verdict = verified.get(claim.id)
            if verdict and verdict[0]:
                claim.verified = True
                claim.confidence = min(claim.confidence, verdict[1])
                supported.append(claim)
        if len(supported) < 4:
            claims = self._fallback_claims(paper_ir, source_blocks)
            return EvidenceLedger(
                claims=claims,
                warnings=["Too few claims passed verification; used translated extractive mode."],
            ), False
        supported = self._translate_verified_claims(supported)
        if len(supported) < 4:
            claims = self._fallback_claims(paper_ir, source_blocks)
            return EvidenceLedger(
                claims=claims,
                warnings=["Too few Chinese claims passed translation verification; used translated extractive mode."],
            ), False
        return EvidenceLedger(claims=supported), True

    def _fallback_claims(self, paper_ir: PaperIR, source_blocks: list[TextBlockIR]) -> list[EvidenceClaimIR]:
        """Create a readable fallback while preventing raw English from reaching the article."""
        claims = self._extractive_claims(paper_ir, source_blocks)
        if not claims or not self._llm.enabled:
            return claims
        translated = self._translate_verified_claims(claims)
        minimum = max(4, min(8, len(claims)))
        if len(translated) < minimum or any(_cjk_ratio(claim.claim) < 0.12 for claim in translated):
            logger.warning("Extractive fallback could not produce enough verified Chinese claims.")
            return []
        return translated

    def _verify_claims(self, claims: list[EvidenceClaimIR]) -> dict[str, tuple[bool, float]] | None:
        if len(claims) > 10:
            batches = [claims[index:index + 10] for index in range(0, len(claims), 10)]
            with ThreadPoolExecutor(max_workers=self._parallel_workers(len(batches))) as executor:
                results = list(executor.map(self._verify_claims, batches))
            if any(result is None for result in results):
                return None
            return {key: value for result in results for key, value in result.items()}
        verifier_model = str(getattr(self._settings, "llm_verifier_model", "") or "").strip() or None
        payload = [
            {
                "id": claim.id,
                "claim": claim.claim,
                "evidence": [item.quote for item in claim.evidence],
            }
            for claim in claims
        ]
        response = self._llm.generate_json(
            system_prompt=(
                "你是独立事实核验员。逐条判断 claim 是否完全被 evidence 支持。"
                "部分支持、夸大、因果误写、数字不一致均判为 false。"
                "逐项绑定数值与其对象、实验条件和样本变化方向，不能只检查数字是否出现。"
                "检查结果归属及适用条件；引文或前期研究的结论不可冒充本文结果，单因子等特殊情形不可扩展到一般模型。"
                "若原文的表格与正文冲突，只有明确注明冲突和数据口径的转述才可通过。只返回 JSON。"
                "明确注明冲突即可，不要求消除论文自身矛盾，禁止要求任选一个值或编造解释。"
            ),
            user_prompt=(
                '返回 {"verdicts":[{"id":"claim-001","supported":true,"confidence":0.0}]}。\n'
                f"待核验内容：{json.dumps(payload, ensure_ascii=False)}"
            ),
            model=verifier_model,
        )
        if not response or not isinstance(response.get("verdicts"), list):
            return None
        verdicts: dict[str, tuple[bool, float]] = {}
        for item in response["verdicts"]:
            if not isinstance(item, dict):
                continue
            claim_id = str(item.get("id") or "")
            if claim_id:
                verdicts[claim_id] = (item.get("supported") is True, _clamp_float(item.get("confidence"), 0.5))
        if set(verdicts) != {claim.id for claim in claims}:
            return None
        return verdicts

    def _translate_verified_claims(self, claims: list[EvidenceClaimIR]) -> list[EvidenceClaimIR]:
        english_claims = [claim for claim in claims if _cjk_ratio(claim.claim) < 0.12]
        if not english_claims:
            return claims
        response = self._llm.generate_json(
            system_prompt=(
                "你是科研声明翻译器。将英文 claim 准确翻译成中文，保留全部数字、单位、限定条件和不确定语气。"
                "不得增加解释或改变因果强度。只返回 JSON。"
            ),
            user_prompt=(
                '返回 {"translations":[{"id":"claim-001","claim":"中文"}]}。\n'
                f"待翻译声明：{json.dumps([{'id': claim.id, 'claim': claim.claim} for claim in english_claims], ensure_ascii=False)}"
            ),
            model=self._article_model,
        )
        if not response or not isinstance(response.get("translations"), list):
            logger.warning("Verified English claims were excluded because translation returned no valid JSON.")
            english_ids = {claim.id for claim in english_claims}
            return [claim for claim in claims if claim.id not in english_ids]

        translations = {
            str(item.get("id")): re.sub(r"\s+", " ", str(item.get("claim") or "")).strip()
            for item in response["translations"]
            if isinstance(item, dict) and item.get("id") and item.get("claim")
        }
        translated: list[EvidenceClaimIR] = []
        original_by_id = {claim.id: claim for claim in claims}
        for claim in english_claims:
            text = translations.get(claim.id, "")
            if not text or _cjk_ratio(text) < 0.12:
                continue
            translated.append(claim.model_copy(update={"claim": text, "verified": False}))
        verdicts = self._verify_claims(translated) if translated else None
        accepted = {
            claim.id: claim.model_copy(update={"verified": True, "confidence": min(claim.confidence, verdicts[claim.id][1])})
            for claim in translated
            if verdicts and verdicts.get(claim.id, (False, 0.0))[0]
        }
        english_ids = {claim.id for claim in english_claims}
        outputs = [claim for claim in claims if claim.id not in english_ids]
        outputs.extend(accepted[claim_id] for claim_id in original_by_id if claim_id in accepted)
        outputs.sort(key=lambda claim: int(claim.id.rsplit("-", 1)[-1]))
        return outputs

    def _extractive_claims(self, paper_ir: PaperIR, source_blocks: list[TextBlockIR]) -> list[EvidenceClaimIR]:
        section_lookup = {section.id: section.title for section in paper_ir.sections}
        chosen: list[tuple[TextBlockIR, str]] = []
        abstract = next((block for block in source_blocks if block.kind == "abstract"), None)
        if abstract:
            for sentence in _sentences(abstract.text)[:4]:
                synthetic = abstract.model_copy(update={"text": sentence})
                chosen.append((synthetic, self._classify_claim(sentence, "Abstract")))

        used_sections: set[str] = set()
        for block in source_blocks:
            if block.kind == "abstract" or block.section_id in used_sections:
                continue
            title = section_lookup.get(block.section_id, "")
            claim_type = self._classify_claim(block.text, title)
            if claim_type in {"method", "data", "empirical_result", "conclusion"}:
                chosen.append((block, claim_type))
                used_sections.add(block.section_id)
            if len(chosen) >= 16:
                break

        claims: list[EvidenceClaimIR] = []
        seen: set[str] = set()
        for block, claim_type in chosen:
            text = block.text.strip()[:1000]
            normalized = _normalize_text(text)
            if not text or normalized in seen:
                continue
            seen.add(normalized)
            claims.append(
                EvidenceClaimIR(
                    id=f"claim-{len(claims) + 1:03d}",
                    claim=text,
                    claim_type=claim_type,
                    evidence=[ClaimEvidenceIR(block_id=block.id, page=block.page, quote=text)],
                    author_asserted=True,
                    confidence=1.0,
                    verified=True,
                )
            )
        return claims

    @staticmethod
    def _classify_claim(text: str, section_title: str) -> str:
        value = f"{section_title} {text[:300]}".lower()
        if re.search(r"data|dataset|sample|observation|数据|样本", value):
            return "data"
        if re.search(r"result|empirical|performance|outperform|experiment|结果|实证|表现", value):
            return "empirical_result"
        if re.search(r"method|model|algorithm|framework|estimat|方法|模型|算法", value):
            return "method"
        if re.search(r"conclusion|we find|we show|结论|发现", value):
            return "conclusion"
        return "contribution"

    def _link_figures_to_claims(self, paper_ir: PaperIR, ledger: EvidenceLedger) -> None:
        claim_lookup = {claim.id: claim for claim in ledger.claims}
        evidence_to_claims: dict[str, list[str]] = defaultdict(list)
        for claim in ledger.claims:
            for evidence in claim.evidence:
                evidence_to_claims[evidence.block_id].append(claim.id)
        for figure in paper_ir.figures:
            linked = []
            for block_id in figure.context_block_ids:
                linked.extend(evidence_to_claims.get(block_id, []))
            figure.related_claim_ids = list(dict.fromkeys(linked))[:8]

        if not self._llm.enabled or not paper_ir.figures or not ledger.claims:
            return
        eligible_by_figure: dict[str, set[str]] = {}
        figure_payload = []
        for figure in paper_ir.figures:
            eligible = {claim.id for claim in ledger.claims if claim.verified}
            eligible_by_figure[figure.id] = eligible
            figure_payload.append(
                {
                    "id": figure.id,
                    "page": figure.page,
                    "caption": figure.caption,
                    "summary": figure.analysis.summary,
                    "candidate_claims": [
                        {"id": claim.id, "claim": claim.claim, "evidence_pages": [item.page for item in claim.evidence]}
                        for claim in ledger.claims
                        if claim.id in eligible
                    ],
                }
            )
        response = self._llm.generate_json(
            system_prompt=(
                "你是论文图文关系标注员。仅根据图注、视觉摘要和已验证声明建立直接关系。"
                "只能从每张图片的 candidate_claims 中选择最多 3 条直接解释该图的声明。"
                "主题宽泛相关但不能解释图片内容的声明不得关联；没有直接声明时返回空数组。只返回 JSON。"
            ),
            user_prompt=(
                '返回 {"links":[{"figure_id":"fig-001","claim_ids":["claim-001"]}]}。\n'
                f"图片及候选声明：{json.dumps(figure_payload, ensure_ascii=False)}"
            ),
            model=self._article_model,
        )
        if not response or not isinstance(response.get("links"), list):
            return
        figure_lookup = {figure.id: figure for figure in paper_ir.figures}
        for item in response["links"]:
            if not isinstance(item, dict):
                continue
            figure = figure_lookup.get(str(item.get("figure_id") or ""))
            values = item.get("claim_ids")
            if not figure or not isinstance(values, list):
                continue
            eligible = eligible_by_figure.get(figure.id, set())
            valid = [str(value) for value in values if str(value) in claim_lookup and str(value) in eligible]
            figure.related_claim_ids = list(dict.fromkeys(valid))[:3]

    def _align_article_figures(self, document: ArticleDocument, paper_ir: PaperIR) -> None:
        figures = {figure.id: figure for figure in paper_ir.figures if figure.selected}
        if not figures:
            return
        paragraphs = {block.id: block for section in document.sections for block in section.blocks if block.type == "paragraph"}
        for attempt in range(2):
            response = self._llm.generate_json(
                system_prompt=("你是论文图文排版审校员。为每张图选择最直接解释其实际内容的正文段落。"
                    "按已核验图注的具体变量、市场状态和比较对象匹配，不能只因为原文同页或主题相近而关联。"
                    "实证结果图不能放在未来工作或无关局限之后。不得改写正文。只返回完整JSON。"),
                user_prompt=('返回 {"placements":[{"figure_id":"...","after_block_id":"...","reason":"直接对应的内容"}]}。'
                    "每个figure_id必须恰好出现一次，after_block_id只能选择给定段落。\n"
                    f"图片：{json.dumps([{'figure_id': f.id, 'caption': f.caption, 'verified_caption': f.analysis.summary} for f in figures.values()], ensure_ascii=False)}\n"
                    f"段落：{json.dumps([{'block_id': b.id, 'section': s.title, 'text': b.text} for s in document.sections for b in s.blocks if b.type == 'paragraph'], ensure_ascii=False)}"),
                model=self._settings.llm_verifier_model, reasoning_budget=4096,
            )
            rows = response.get("placements") if isinstance(response, dict) else None
            if not isinstance(rows, list) or len(rows) != len(figures):
                continue
            if any(not isinstance(row, dict) or row.get("figure_id") not in figures or row.get("after_block_id") not in paragraphs or not str(row.get("reason", "")).strip() for row in rows):
                continue
            if {row["figure_id"] for row in rows} != set(figures):
                continue
            by_anchor = defaultdict(list)
            for row in rows:
                by_anchor[row["after_block_id"]].append(row["figure_id"])
            figure_blocks = {b.asset_id: b for s in document.sections for b in s.blocks if b.type == "figure"}
            if set(figure_blocks) != set(figures):
                raise RuntimeError("Selected figures differ from article figure blocks")
            for section in document.sections:
                rebuilt = []
                for block in section.blocks:
                    if block.type == "figure":
                        continue
                    rebuilt.append(block)
                    rebuilt.extend(figure_blocks[fig] for fig in sorted(by_anchor.get(block.id, [])))
                section.blocks = rebuilt
            return
        raise RuntimeError("Figure placement review returned incomplete or invalid anchors")

    def _select_figures(self, paper_ir: PaperIR) -> None:
        result_words = re.compile(r"result|performance|comparison|forecast|return|error|结果|表现|比较|预测", re.I)
        method_words = re.compile(r"method|model|architecture|framework|algorithm|方法|模型|框架|算法", re.I)
        for figure in paper_ir.figures:
            figure.selected = False
            reference_score = min(1.0, figure.reference_count / 3.0)
            caption_score = 1.0 if result_words.search(figure.caption) else (0.75 if method_words.search(figure.caption) else 0.45)
            link_score = min(1.0, len(figure.related_claim_ids) / 2.0)
            source_score = 0.4 if figure.source_kind == "caption_region_fallback" else 1.0
            vision_score = 1.0 if figure.analysis.vision_verified else 0.45
            figure.score = round(
                0.30 * figure.analysis.importance
                + 0.18 * reference_score
                + 0.14 * figure.analysis.legibility
                + 0.12 * caption_score
                + 0.14 * link_score
                + 0.07 * vision_score
                + 0.05 * source_score,
                4,
            )
            if figure.analysis.decorative or figure.analysis.legibility < 0.35 or (
                self._settings.article_require_verified and self._llm.enabled
                and not (figure.analysis.vision_verified and figure.analysis.caption_verified)
            ):
                figure.score = 0.0

        min_images = max(0, int(getattr(self._settings, "article_min_images", 1)))
        max_images = max(min_images, int(getattr(self._settings, "article_max_images", 4)))
        threshold = float(getattr(self._settings, "article_min_figure_score", 0.56))
        ranked = sorted(paper_ir.figures, key=lambda figure: figure.score, reverse=True)
        selected: list[FigureIR] = []
        for figure in ranked:
            if figure.score < threshold or not figure.related_claim_ids:
                continue
            if any(_figures_redundant(figure, existing) for existing in selected):
                continue
            selected.append(figure)
            if len(selected) >= max_images:
                break
        if len(selected) < min_images:
            fallbacks = [
                figure
                for figure in ranked
                if figure not in selected
                and figure.score >= 0.38
                and not figure.analysis.decorative
                and figure.related_claim_ids
                and not any(_figures_redundant(figure, existing) for existing in selected)
            ]
            selected.extend(fallbacks[: min_images - len(selected)])
        for figure in selected:
            figure.selected = True

    def _build_article_plan(self, paper_ir: PaperIR, ledger: EvidenceLedger) -> ArticlePlan:
        selected_figures = [figure for figure in paper_ir.figures if figure.selected]
        response = None
        if self._llm.enabled:
            response = self._llm.generate_json(
                system_prompt=(
                    "你是证据驱动的中文论文解读编辑。先规划文章，不写正文。"
                    "每个章节只能使用给定 claim_id。图片必须放在直接解释它的声明之后。"
                    "相同或近义声明只选一个代表，每条声明和每个公式只能归入一个章节，禁止反复改写同一结论。"
                    "先解释研究问题和模型/数据设定，再展开方法、条件、主要结果与原文验证。"
                    "章节必须各自提供新信息，不要把同一误差分解、同一比较结论拆成多个空泛章节。"
                    "不要用研究背景、应用意义或重复的结论章节占篇幅，应覆盖模型条件、推导步骤、可估计量和验证设计。"
                    "理论论文不得编造实证章节；每章分配 4 到 10 条有实质信息的声明，避免只有一个空泛声明的章节。"
                    "除非存在 future_work 声明，否则禁止规划未来研究、下一步建议或展望。只返回 JSON。"
                ),
                model=self._article_model,
                user_prompt=(
                    "规划 4 到 7 个适合该论文的章节，返回："
                    '{"title":"中文标题", "sections":[{"title":"", "purpose":"", '
                    '"claim_ids":[], "figure_placements":[{"figure_id":"", "after_claim_id":"", "reason":""}], '
                    '"equation_ids":[], "target_words":450}]}。\n'
                    f"论文标题：{paper_ir.metadata.title}\n"
                    f"声明：{json.dumps([{'id': c.id, 'type': c.claim_type, 'claim': c.claim} for c in ledger.claims], ensure_ascii=False)}\n"
                    f"图片：{json.dumps([{'id': f.id, 'caption': f.caption, 'related_claim_ids': f.related_claim_ids} for f in selected_figures], ensure_ascii=False)}\n"
                    f"公式：{json.dumps([{'id': e.id, 'latex': e.latex, 'context': e.context[:600]} for e in paper_ir.equations[:24]], ensure_ascii=False)}"
                ),
            )
        plan = self._validated_plan(response, paper_ir, ledger)
        self._ensure_figure_placements(plan, paper_ir, ledger)
        return plan

    def _validated_plan(
        self,
        payload: dict[str, Any] | None,
        paper_ir: PaperIR,
        ledger: EvidenceLedger,
    ) -> ArticlePlan:
        claim_lookup = {claim.id: claim for claim in ledger.claims}
        figure_ids = {figure.id for figure in paper_ir.figures if figure.selected}
        figure_claims = {figure.id: set(figure.related_claim_ids) for figure in paper_ir.figures}
        equation_ids = {equation.id for equation in paper_ir.equations}
        sections: list[ArticleSectionPlanIR] = []
        used_claims: set[str] = set()
        used_equations: set[str] = set()
        future_allowed = any(_is_explicit_future_claim(claim) for claim in ledger.claims)
        if payload and isinstance(payload.get("sections"), list):
            for item in payload["sections"][:7]:
                if not isinstance(item, dict):
                    continue
                title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()[:80]
                if not title or (not future_allowed and re.search(r"未来|展望|下一步|future", title, re.I)):
                    continue
                values = item.get("claim_ids")
                if not isinstance(values, list):
                    continue
                claim_ids = [str(value) for value in values if str(value) in claim_lookup and str(value) not in used_claims]
                claim_ids = list(dict.fromkeys(claim_ids))
                if not claim_ids:
                    continue
                placements: list[FigurePlacementIR] = []
                raw_placements = item.get("figure_placements")
                if isinstance(raw_placements, list):
                    for placement in raw_placements:
                        if not isinstance(placement, dict):
                            continue
                        figure_id = str(placement.get("figure_id") or "")
                        after_claim_id = str(placement.get("after_claim_id") or "")
                        if (
                            figure_id in figure_ids
                            and after_claim_id in claim_ids
                            and after_claim_id in figure_claims.get(figure_id, set())
                        ):
                            placements.append(
                                FigurePlacementIR(
                                    figure_id=figure_id,
                                    after_claim_id=after_claim_id,
                                    reason=str(placement.get("reason") or "")[:300],
                                )
                            )
                raw_equations = item.get("equation_ids")
                valid_equations = (
                    list(dict.fromkeys(str(value) for value in raw_equations if str(value) in equation_ids and str(value) not in used_equations))[:3]
                    if isinstance(raw_equations, list)
                    else []
                )
                sections.append(
                    ArticleSectionPlanIR(
                        id=f"article-sec-{len(sections) + 1:02d}",
                        title=title,
                        purpose=str(item.get("purpose") or title)[:300],
                        claim_ids=claim_ids,
                        figure_placements=placements,
                        equation_ids=valid_equations,
                        target_words=max(180, min(900, int(item.get("target_words") or 450))),
                    )
                )
                used_claims.update(claim_ids)
                used_equations.update(valid_equations)

        if not sections:
            sections = self._fallback_plan_sections(ledger)
            used_claims = {claim_id for section in sections for claim_id in section.claim_ids}

        covered_types = {claim_lookup[claim_id].claim_type for claim_id in used_claims}
        important = [claim for claim in ledger.claims if claim.claim_type in {"method", "data", "empirical_result", "numeric_result", "conclusion"}]
        # Only restore entirely missing evidence categories. Unselected claims may
        # intentionally be paraphrases or appendix details, not omissions to pad out.
        uncovered = [claim for claim in important if claim.claim_type not in covered_types][:8]
        if uncovered:
            grouped: dict[str, list[str]] = defaultdict(list)
            for claim in uncovered:
                grouped[claim.claim_type].append(claim.id)
            title_map = {
                "method": "补充方法细节",
                "data": "补充数据说明",
                "empirical_result": "补充实证结果",
                "numeric_result": "关键数值结果",
                "conclusion": "主要结论",
            }
            for claim_type, claim_ids in grouped.items():
                sections.append(
                    ArticleSectionPlanIR(
                        id=f"article-sec-{len(sections) + 1:02d}",
                        title=title_map[claim_type],
                        purpose="覆盖尚未进入文章计划的已验证核心证据。",
                        claim_ids=claim_ids,
                        target_words=300,
                    )
                )

        raw_title = str(payload.get("title") if payload else "").strip()
        title = raw_title or paper_ir.metadata.title
        title = re.sub(r"^#?\s*arxiv论文速递\|", "", title, flags=re.I).strip()
        return ArticlePlan(title=f"arxiv论文速递|{title}", sections=sections[:8])

    @staticmethod
    def _fallback_plan_sections(ledger: EvidenceLedger) -> list[ArticleSectionPlanIR]:
        groups = [
            ("研究问题与贡献", {"research_question", "contribution"}),
            ("方法论", {"method", "assumption"}),
            ("数据与研究设计", {"data"}),
            ("实证结果", {"empirical_result", "numeric_result"}),
            ("主要结论", {"conclusion"}),
            ("作者明确说明的局限", {"limitation"}),
            ("作者提出的后续研究", {"future_work"}),
        ]
        sections: list[ArticleSectionPlanIR] = []
        assigned: set[str] = set()
        for title, types in groups:
            claim_ids = [claim.id for claim in ledger.claims if claim.claim_type in types]
            if not claim_ids:
                continue
            assigned.update(claim_ids)
            sections.append(
                ArticleSectionPlanIR(
                    id=f"article-sec-{len(sections) + 1:02d}",
                    title=title,
                    purpose=f"依据已验证证据说明{title}。",
                    claim_ids=claim_ids[:10],
                    target_words=450,
                )
            )
        remaining = [claim.id for claim in ledger.claims if claim.id not in assigned]
        if remaining:
            sections.insert(
                0,
                ArticleSectionPlanIR(
                    id="article-sec-00",
                    title="论文概览",
                    purpose="概括论文原文明示的核心内容。",
                    claim_ids=remaining[:8],
                    target_words=350,
                ),
            )
        return sections

    @staticmethod
    def _ensure_figure_placements(plan: ArticlePlan, paper_ir: PaperIR, ledger: EvidenceLedger) -> None:
        placed: set[str] = set()
        claim_to_section: dict[str, ArticleSectionPlanIR] = {}
        for section in plan.sections:
            for claim_id in section.claim_ids:
                claim_to_section.setdefault(claim_id, section)
            deduped = []
            for placement in section.figure_placements:
                if placement.figure_id not in placed:
                    placed.add(placement.figure_id)
                    deduped.append(placement)
            section.figure_placements = deduped

        for figure in paper_ir.figures:
            if not figure.selected or figure.id in placed:
                continue
            anchor = next((claim_id for claim_id in figure.related_claim_ids if claim_id in claim_to_section), None)
            if not anchor:
                anchor = next((claim_id for claim_id in figure.related_claim_ids if any(claim.id == claim_id for claim in ledger.claims)), None)
                if not anchor or not plan.sections:
                    figure.selected = False
                    continue
                section = next((item for item in plan.sections if re.search(r"模拟|实证|结果|验证", item.title)), plan.sections[-1])
                section.claim_ids.append(anchor)
                claim_to_section[anchor] = section
            section = claim_to_section[anchor]
            section.figure_placements.append(
                FigurePlacementIR(
                    figure_id=figure.id,
                    after_claim_id=anchor,
                    reason="图片与该已验证声明共享原文上下文。",
                )
            )
            placed.add(figure.id)

    def _draft_document(
        self,
        paper_ir: PaperIR,
        ledger: EvidenceLedger,
        plan: ArticlePlan,
        *, revision_issues: list[QualityIssueIR] | None = None,
    ) -> tuple[ArticleDocument, bool]:
        claim_lookup = {claim.id: claim for claim in ledger.claims}
        equation_lookup = {equation.id: equation for equation in paper_ir.equations}
        sections: list[ArticleSectionIR] = []
        complete = self._llm.enabled
        entries = [
            (
                section_plan,
                [claim_lookup[claim_id] for claim_id in section_plan.claim_ids if claim_id in claim_lookup],
            )
            for section_plan in plan.sections
        ]

        def draft(entry: tuple[ArticleSectionPlanIR, list[EvidenceClaimIR]]) -> dict[str, Any] | None:
            section_plan, claims = entry
            if self._llm.enabled and claims:
                return self._llm.generate_json(
                    system_prompt=(
                        "你是证据约束的中文科技编辑。只能使用提供的 claims 和 evidence 写当前章节。"
                        "每个段落必须列出实际使用的 claim_ids。原文 evidence 优先于旧 claims，修正声明中的翻译错误。不要增加常识、建议、局限或未来方向。"
                        "PDF表格可能跨行错位；原始TeX的 & 列和双反斜杠行提供准确的对象与数值对应，须优先按此解释表格。"
                        "遵守整篇文章分工，不复述其他章节内容。同一段落只写一次，同一事实不要换说法凑字数。"
                        "优先解释原文明确给出的变量、假设、估计步骤、样本处理和结果数值，删除空泛评价。"
                        "始终保留结果归属及适用条件，原文引用的既有研究不可写成本文结论，结论段不可推广特殊情形。"
                        "区分作者结论与解释，不输出 Markdown 标题或图片。复杂公式只通过 equation_ids_after 插入；"
                        "正文优先用中文解释变量，必要的行内数学必须用 $...$ 包裹，禁止裸露的下划线、幂次花括号或 LaTeX 命令。"
                        "claim-001、eq-002 等内部标识只能出现在 JSON 的 ID 字段，绝不能写进正文；需要指代公式时使用变量含义或下式。"
                        "只有段落直接解释某个给定公式时，才在 equation_ids_after 中指定其 ID；不得将不相关公式放在段落后。只返回 JSON。"
                    ),
                    model=self._article_model,
                    user_prompt=(
                        ("上次稿件未通过审核。本次必须根据原文纠正以下问题，不能复用错误结论或在正文描述修订过程："
                         + json.dumps([i.model_dump() for i in revision_issues], ensure_ascii=False)[:16000] + "\n" if revision_issues else "") +
                        f"章节：{section_plan.title}\n目的：{section_plan.purpose}\n"
                        f"全文分工：{json.dumps([{'title': s.title, 'purpose': s.purpose} for s in plan.sections], ensure_ascii=False)}\n"
                        f"本章原文公式：{json.dumps([{'id': e, 'latex': equation_lookup[e].latex, 'context': equation_lookup[e].context} for e in section_plan.equation_ids if e in equation_lookup], ensure_ascii=False)}\n"
                        f"原始TeX表格与定理：{json.dumps(paper_ir.structured_source, ensure_ascii=False)[:100000]}\n"
                        f"目标字数：{max(700, section_plan.target_words)}；输出至少 3 个各 180 到 260 字的中文段落，覆盖该章节绑定 claims 的方法、变量定义、数据、推导逻辑、结果与限制条件，不用空泛套话凑字数。\n"
                        '返回 {"blocks":[{"text":"中文段落", "claim_ids":["claim-001"], "equation_ids_after":[]}]}。'
                        "每段聚焦一个论点，最多使用 3 个声明；只写论文已完成的研究，不写下一步方向。\n"
                        f"证据：{json.dumps([claim.model_dump() for claim in claims], ensure_ascii=False)}"
                    ),
                )
            return None

        workers = self._parallel_workers(len(entries))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="autosci-draft") as executor:
            responses = list(executor.map(draft, entries))

        for (section_plan, claims), response in zip(entries, responses):
            blocks: list[ArticleBlockIR] = []
            inserted_equations: set[str] = set()
            allowed_ids = {claim.id for claim in claims}
            if response and isinstance(response.get("blocks"), list):
                for item in response["blocks"][:10]:
                    if not isinstance(item, dict):
                        continue
                    text = _normalize_inline_math(re.sub(r"\s+", " ", str(item.get("text") or "")).strip())
                    values = item.get("claim_ids")
                    claim_ids = [str(value) for value in values if str(value) in allowed_ids] if isinstance(values, list) else []
                    claim_ids = list(dict.fromkeys(claim_ids))
                    if text and claim_ids:
                        blocks.append(
                            ArticleBlockIR(
                                id=f"block-{section_plan.id}-{len(blocks) + 1:02d}",
                                text=text[:2400],
                                claim_ids=claim_ids,
                            )
                        )
                        requested_equations = item.get("equation_ids_after")
                        if isinstance(requested_equations, list):
                            for equation_id in requested_equations:
                                if equation_id not in section_plan.equation_ids or equation_id in inserted_equations:
                                    continue
                                equation = equation_lookup[equation_id]
                                if _balanced_latex(equation.latex):
                                    blocks.append(ArticleBlockIR(
                                        id=f"block-{section_plan.id}-{equation.id}", type="equation",
                                        text=equation.latex, asset_id=equation.id, claim_ids=claim_ids,
                                    ))
                                    inserted_equations.add(equation_id)
            if not blocks:
                complete = False
                blocks = self._extractive_section_blocks(section_plan.id, claims)

            for placement in section_plan.figure_placements:
                insert_at = next(
                    (
                        index + 1
                        for index, block in enumerate(blocks)
                        if block.type == "paragraph" and placement.after_claim_id in block.claim_ids
                    ),
                    None,
                )
                if insert_at is None:
                    claim = claim_lookup.get(placement.after_claim_id)
                    if not claim:
                        continue
                    blocks.append(
                        ArticleBlockIR(
                            id=f"block-{section_plan.id}-anchor-{claim.id}",
                            text=claim.claim,
                            claim_ids=[claim.id],
                        )
                    )
                    insert_at = len(blocks)
                blocks.insert(
                    insert_at,
                    ArticleBlockIR(
                        id=f"block-{section_plan.id}-{placement.figure_id}",
                        type="figure",
                        asset_id=placement.figure_id,
                    ),
                )
            sections.append(ArticleSectionIR(id=section_plan.id, title=section_plan.title, blocks=blocks))

        return (
            ArticleDocument(
                title=plan.title,
                authors=paper_ir.metadata.authors,
                arxiv_url=paper_ir.metadata.url,
                sections=sections,
            ),
            complete,
        )

    @staticmethod
    def _extractive_section_blocks(section_id: str, claims: list[EvidenceClaimIR]) -> list[ArticleBlockIR]:
        return [
            ArticleBlockIR(
                id=f"block-{section_id}-{index:02d}",
                text=claim.claim,
                claim_ids=[claim.id],
            )
            for index, claim in enumerate(claims, start=1)
        ]

    def _verify_and_repair(
        self,
        document: ArticleDocument,
        paper_ir: PaperIR,
        ledger: EvidenceLedger,
        plan: ArticlePlan,
        *,
        evidence_verified: bool,
        drafting_complete: bool,
        previous_issues: list[QualityIssueIR] | None = None,
        checkpoint_dir: Path | None = None,
    ) -> tuple[ArticleDocument, QualityReport]:
        if self._settings.article_require_verified and self._llm.enabled:
            return self._verify_strict_document(
                document, paper_ir, ledger, plan,
                evidence_verified=evidence_verified, drafting_complete=drafting_complete,
                previous_issues=previous_issues,
                checkpoint_dir=checkpoint_dir,
            )
        claim_lookup = {claim.id: claim for claim in ledger.claims}
        paragraph_blocks = [
            block
            for section in document.sections
            for block in section.blocks
            if block.type == "paragraph"
        ]
        verifier_model = str(getattr(self._settings, "llm_verifier_model", "") or "").strip() or None
        llm_verdicts = None
        verification_unavailable = False
        repair_notes: list[QualityIssueIR] = []
        context_issues: list[QualityIssueIR] = []
        context_verified = False

        def use_verified_claim_text(block: ArticleBlockIR, reason: str) -> None:
            block.text = " ".join(
                claim_lookup[claim_id].claim for claim_id in block.claim_ids if claim_id in claim_lookup
            )
            repair_notes.append(
                QualityIssueIR(
                    code="paragraph_replaced_with_verified_claims",
                    message=reason,
                    severity="warning",
                    block_id=block.id,
                )
            )

        if self._llm.enabled and paragraph_blocks:
            llm_verdicts = self._verify_article_blocks(paragraph_blocks, claim_lookup, verifier_model)
        if llm_verdicts is not None:
            max_repairs = max(0, int(getattr(self._settings, "article_max_repair_attempts", 2)))
            for block in paragraph_blocks:
                verdict = llm_verdicts.get(block.id)
                if not verdict or verdict[0]:
                    continue
                repaired = False
                for _ in range(max_repairs):
                    replacement = self._repair_block(block, claim_lookup, verdict[1])
                    if not replacement:
                        continue
                    block.text = replacement
                    followup = self._verify_article_blocks([block], claim_lookup, verifier_model)
                    if followup and followup.get(block.id, (False, []))[0]:
                        repaired = True
                        break
                if not repaired:
                    use_verified_claim_text(block, "Paragraph repair did not pass; used independently verified claims.")
        elif drafting_complete:
            verification_unavailable = True

        if llm_verdicts is not None:
            followup_blocks = [
                block
                for section in document.sections
                for block in section.blocks
                if block.type == "paragraph"
            ]
            followup = self._verify_article_blocks(followup_blocks, claim_lookup, verifier_model)
            if followup is None:
                verification_unavailable = True
            else:
                for block_id, verdict in followup.items():
                    if not verdict[0]:
                        block = next((item for item in followup_blocks if item.id == block_id), None)
                        if block:
                            reason = "; ".join(verdict[1])[:600] or "Paragraph was not fully supported."
                            use_verified_claim_text(block, reason)

        strict_quality = (
            bool(getattr(self._settings, "article_require_verified", True))
            and self._llm.enabled
        )
        if strict_quality and llm_verdicts is not None and not verification_unavailable:
            review = self._review_article_context(document, paper_ir, verifier_model)
            if review is None:
                verification_unavailable = True
            else:
                failed = {block_id: verdict[1] for block_id, verdict in review.items() if not verdict[0]}
                for block in paragraph_blocks:
                    if block.id in failed:
                        replacement = self._repair_block(block, claim_lookup, failed[block.id])
                        if replacement:
                            block.text = replacement
                if failed:
                    review = self._review_article_context(document, paper_ir, verifier_model)
                if review is None:
                    verification_unavailable = True
                else:
                    context_issues = [QualityIssueIR(code="article_context_mismatch", message="; ".join(verdict[1])[:600] or "全文一致性审校未通过。", block_id=block_id) for block_id, verdict in review.items() if not verdict[0]]
                    context_verified = not context_issues
        if strict_quality and drafting_complete and llm_verdicts is not None and not verification_unavailable:
            self._stabilize_verified_document(
                document,
                ledger,
                min_paragraphs=int(getattr(self._settings, "article_min_paragraphs", 8)),
                min_chars=int(getattr(self._settings, "article_min_chars", 1800)),
            )
        issues = self._deterministic_issues(
            document,
            paper_ir,
            ledger,
            enforce_chinese=evidence_verified or strict_quality,
            strict_quality=strict_quality,
            min_paragraphs=int(getattr(self._settings, "article_min_paragraphs", 8)),
            min_sections=int(getattr(self._settings, "article_min_sections", 4)),
            min_chars=int(getattr(self._settings, "article_min_chars", 1800)),
        )
        for issue in list(issues):
            if issue.severity != "error" or not issue.block_id:
                continue
            block = next((item for item in paragraph_blocks if item.id == issue.block_id), None)
            if block:
                use_verified_claim_text(block, f"Deterministic gate {issue.code} required an evidence-only replacement.")
        if strict_quality and drafting_complete and llm_verdicts is not None and not verification_unavailable:
            self._stabilize_verified_document(
                document,
                ledger,
                min_paragraphs=int(getattr(self._settings, "article_min_paragraphs", 8)),
                min_chars=int(getattr(self._settings, "article_min_chars", 1800)),
            )
        issues = self._deterministic_issues(
            document,
            paper_ir,
            ledger,
            enforce_chinese=evidence_verified or strict_quality,
            strict_quality=strict_quality,
            min_paragraphs=int(getattr(self._settings, "article_min_paragraphs", 8)),
            min_sections=int(getattr(self._settings, "article_min_sections", 4)),
            min_chars=int(getattr(self._settings, "article_min_chars", 1800)),
        )
        if verification_unavailable:
            issues.append(
                QualityIssueIR(
                    code="article_verifier_unavailable",
                    message="Independent article verification was unavailable; switched to extractive fallback.",
                    severity="error",
                )
            )

        issues.extend(context_issues)

        error_count = sum(issue.severity == "error" for issue in issues)
        mode = "verified" if evidence_verified and drafting_complete and llm_verdicts is not None else "extractive_fallback"
        if strict_quality and mode == "extractive_fallback":
            issues.append(
                QualityIssueIR(
                    code="verified_article_required",
                    message="证据抽取、翻译、段落生成或独立核验未完整通过，拒绝将 fallback 文章进入发布队列。",
                    severity="error",
                )
            )
        if error_count and not strict_quality:
            fallback_reasons = [issue for issue in issues if issue.severity == "error"]
            document = self._force_extractive_document(document, ledger, paper_ir)
            residual = self._deterministic_issues(
                document,
                paper_ir,
                ledger,
                enforce_chinese=evidence_verified or strict_quality,
                strict_quality=strict_quality,
                min_paragraphs=int(getattr(self._settings, "article_min_paragraphs", 8)),
                min_sections=int(getattr(self._settings, "article_min_sections", 4)),
                min_chars=int(getattr(self._settings, "article_min_chars", 1800)),
            )
            issues = [issue for issue in residual if issue.severity == "error"]
            issues.extend(repair_notes)
            issues.extend(
                QualityIssueIR(
                    code=f"fallback_reason_{issue.code}",
                    message=issue.message,
                    severity="warning",
                    block_id=issue.block_id,
                )
                for issue in fallback_reasons
            )
            issues.append(
                QualityIssueIR(
                    code="safe_extractive_fallback",
                    message="Generated prose did not pass all gates and was replaced with evidence-bound extractive text.",
                    severity="warning",
                )
            )
            mode = "extractive_fallback"
        else:
            issues.extend(repair_notes)

        if strict_quality and mode == "extractive_fallback" and not any(
            issue.code == "verified_article_required" and issue.severity == "error" for issue in issues
        ):
            issues.append(
                QualityIssueIR(
                    code="verified_article_required",
                    message="fallback 文章不得进入发布队列，必须完成证据翻译、段落生成和独立核验。",
                    severity="error",
                )
            )

        metrics = self._quality_metrics(document, paper_ir, ledger)
        metrics["replaced_paragraph_count"] = len({issue.block_id for issue in repair_notes if issue.block_id})
        metrics["article_context_verified"] = context_verified
        final_errors = sum(issue.severity == "error" for issue in issues)
        score = max(0.0, 1.0 - final_errors * 0.25 - (0.08 if mode == "extractive_fallback" else 0.0))
        threshold = float(getattr(self._settings, "article_quality_threshold", 0.90))
        passed = final_errors == 0 and score >= threshold
        if not passed:
            issues.append(
                QualityIssueIR(
                    code="quality_threshold_not_met",
                    message=f"Quality score {score:.2f} is below configured threshold {threshold:.2f}.",
                )
            )
        report = QualityReport(
            passed=passed,
            score=round(score, 4),
            mode=mode if passed else "failed",
            metrics=metrics,
            issues=issues,
            warnings=[*paper_ir.parse_warnings, *ledger.warnings],
        )
        if not report.passed:
            raise ArticleQualityError(document, report)
        return document, report

    def _verify_strict_document(
        self, document: ArticleDocument, paper_ir: PaperIR, ledger: EvidenceLedger,
        plan: ArticlePlan, *, evidence_verified: bool, drafting_complete: bool,
        previous_issues: list[QualityIssueIR] | None = None,
        checkpoint_dir: Path | None = None,
    ) -> tuple[ArticleDocument, QualityReport]:
        # Review the final bytes, never a pre-repair version or an extractive substitute.
        for section in document.sections:
            for block in section.blocks:
                if block.type == "paragraph":
                    block.text = _normalize_inline_math(block.text)
        pending_captions = [figure for figure in paper_ir.figures if figure.selected and not figure.analysis.caption_verified]
        if pending_captions:
            self._verify_figure_captions(pending_captions)
        verifier = self._settings.llm_verifier_model
        source_lookup = {block.id: index for index, block in enumerate(paper_ir.blocks)}
        contextual = ledger.model_copy(deep=True)
        equations = {equation.id: equation for equation in paper_ir.equations}
        for section in document.sections:
            for block in section.blocks:
                if block.type != "equation" or block.asset_id not in equations:
                    continue
                equation = equations[block.asset_id]
                for claim in contextual.claims:
                    if claim.id in block.claim_ids:
                        claim.evidence.append(ClaimEvidenceIR(block_id=equation.id, page=1, quote=equation.context + "\n" + equation.latex))
        for claim in contextual.claims:
            source_ids = {item.block_id for item in claim.evidence}
            for evidence in list(claim.evidence):
                index = source_lookup.get(evidence.block_id)
                if index is None:
                    continue
                # The ledger stores bounded quotes for prompt size. Strict
                # review must use the complete parsed paragraph, otherwise a
                # supported sentence can be rejected solely because its tail
                # was outside the bounded quote.
                evidence.quote = paper_ir.blocks[index].text
                for adjacent in paper_ir.blocks[max(0, index - 1):index + 2]:
                    if adjacent.id not in source_ids and adjacent.kind not in {"heading", "caption"}:
                        claim.evidence.append(ClaimEvidenceIR(block_id=adjacent.id, page=adjacent.page, quote=adjacent.text))
                        source_ids.add(adjacent.id)
                if re.search(r"\b(?:tables?|assumptions?|theorems?|lemmas?|propositions?|corollar(?:y|ies))\s+\d|(?:表|假设|定理|引理)\s*\d", evidence.quote, re.I):
                    for source_block in paper_ir.blocks:
                        if source_block.page == evidence.page and source_block.id not in source_ids:
                            claim.evidence.append(ClaimEvidenceIR(block_id=source_block.id, page=source_block.page, quote=source_block.text))
                            source_ids.add(source_block.id)
        if paper_ir.structured_source:
            structured_text = "\n\n".join(paper_ir.structured_source)[:100000]
            for claim in contextual.claims:
                claim.evidence.append(ClaimEvidenceIR(block_id="tex-structured-source", page=1,
                    quote="Use these original TeX tables and theorem statements to resolve PDF extraction ordering, not to override the original paper. Do not copy internal labels into reader prose.\n" + structured_text))
        lookup = {claim.id: claim for claim in contextual.claims}
        rounds = max(0, self._settings.article_max_repair_attempts) + 1
        issues: list[QualityIssueIR] = []
        verified_text: dict[str, str] = {}
        unresolved_text: dict[str, tuple[str, list[QualityIssueIR]]] = {}
        semantic_codes = {"unsupported_paragraph", "article_context_mismatch", "article_global_mismatch"}
        for section in document.sections:
            for block in section.blocks:
                previous = [issue for issue in previous_issues or [] if issue.block_id == block.id and issue.code in semantic_codes]
                if previous:
                    unresolved_text[block.id] = (block.text, previous)
        repairs = 0
        prior_round_made_no_progress = False
        context_verified = False
        global_verified = False

        def checkpoint(pending: list[QualityIssueIR]) -> None:
            if checkpoint_dir is None:
                return
            self._write_json(checkpoint_dir / "rejected_document.json", document)
            self._write_json(checkpoint_dir / "qa_report.json", QualityReport(
                passed=False, mode="failed", score=0.0,
                metrics={**self._quality_metrics(document, paper_ir, ledger), "article_context_verified": False, "article_global_verified": False},
                issues=[*pending, QualityIssueIR(code="verification_incomplete", message="Automatic verification has not completed; never publish this checkpoint.")],
            ))

        for attempt in range(rounds):
            paragraphs = [block for section in document.sections for block in section.blocks if block.type == "paragraph"]
            issues = self._deterministic_issues(
                document, paper_ir, contextual, strict_quality=True,
                min_paragraphs=self._settings.article_min_paragraphs,
                min_sections=self._settings.article_min_sections,
                min_chars=self._settings.article_min_chars,
            )
            changed = [block for block in paragraphs if verified_text.get(block.id) != block.text]
            checkpoint([*issues, *(issue for block in paragraphs for text, known in [unresolved_text.get(block.id, ("", []))] if text == block.text for issue in known)])
            logger.info("Article verification round %d/%d: %d changed paragraphs, %d deterministic issues", attempt + 1, rounds, len(changed), len(issues))
            verdicts = self._verify_article_blocks(changed, lookup, verifier) if changed else {}
            if verdicts is None:
                issues.append(QualityIssueIR(code="article_verifier_unavailable", message="Paragraph verification returned incomplete verdicts."))
                break
            for block in changed:
                verdict = verdicts.get(block.id, (False, ["Missing paragraph verdict"]))
                if verdict[0]:
                    verified_text[block.id] = block.text
                else:
                    issues.append(QualityIssueIR(code="unsupported_paragraph", message="; ".join(verdict[1])[:1200], block_id=block.id))
            # Context review includes the evidence adjacent to each paragraph's source,
            # avoiding false negatives from unrelated balanced-source truncation.
            context_verified = False
            review = self._review_article_context(document, paper_ir, verifier, contextual)
            if review is None:
                issues.append(QualityIssueIR(code="article_context_unavailable", message="Full-document review returned incomplete verdicts."))
                break
            context_verified = all(verdict[0] for verdict in review.values())
            for block_id, verdict in review.items():
                if not verdict[0]:
                    issues.append(QualityIssueIR(code="article_context_mismatch", message="; ".join(verdict[1])[:1200], block_id=block_id))
            if not evidence_verified or not drafting_complete:
                issues.append(QualityIssueIR(code="verified_article_required", message="Evidence and drafting must both complete successfully."))
            global_verified = False
            global_review = self._review_global_consistency(document, contextual, verifier)
            if global_review is None:
                issues.append(QualityIssueIR(code="article_global_unavailable", message="Global conclusion and attribution audit was unavailable."))
            else:
                issues.extend(global_review)
                global_verified = not global_review
            # A later optimistic verdict must not erase a known defect in bytes
            # that the repair step never changed.
            for block in paragraphs:
                previous = unresolved_text.get(block.id)
                if previous and previous[0] == block.text:
                    current = {(issue.code, issue.message, issue.block_id) for issue in issues}
                    issues.extend(issue for issue in previous[1] if (issue.code, issue.message, issue.block_id) not in current)
                block_issues = [issue for issue in issues if issue.block_id == block.id]
                if block_issues:
                    unresolved_text[block.id] = (block.text, block_issues)
                    verified_text.pop(block.id, None)
                else:
                    unresolved_text.pop(block.id, None)
            if not issues:
                try:
                    self._align_article_figures(document, paper_ir)
                except RuntimeError as exc:
                    issues.append(QualityIssueIR(code="figure_placement_unverified", message=str(exc)))
                else:
                    metrics = self._quality_metrics(document, paper_ir, ledger)
                    metrics.update(article_context_verified=True, article_global_verified=global_verified, replaced_paragraph_count=repairs)
                    report = QualityReport(passed=True, mode="verified", score=1.0, metrics=metrics,
                        warnings=[*paper_ir.parse_warnings, *ledger.warnings])
                    return document, report
            logger.warning("Article verification issues: %s", "; ".join(f"{i.block_id or 'document'}: {i.message}" for i in issues)[:6000])
            checkpoint(issues)
            if attempt + 1 == rounds:
                break
            by_block: dict[str, list[str]] = defaultdict(list)
            for issue in issues:
                if issue.block_id:
                    by_block[issue.block_id].append(issue.message)
            if prior_round_made_no_progress:
                for messages in by_block.values():
                    messages.append(
                        "上一轮修订没有改变原段落。请根据上述证据和问题给出实质性改写；"
                        "不要复述原文，不要返回相同段落。若证据不足以安全修订，则明确保留可证实内容并删除无法支持的部分。"
                    )
            if any(issue.code == "article_char_count" for issue in issues) and paragraphs:
                short_blocks = sorted(paragraphs, key=lambda block: len(block.text))[:3]
                deficit = max(0, self._settings.article_min_chars - sum(len(block.text.strip()) for block in paragraphs))
                increment = math.ceil(deficit / len(short_blocks)) + 60
                for block in short_blocks:
                    by_block[block.id].append(
                        f"全文深度不足。将该段扩展到至少 {len(block.text) + increment} 字，补充绑定原文已经明确的变量定义、假设、步骤或适用条件；"
                        "不得重复结论凑字数，不得补充常识或新事实。证据不足时保留原段落。"
                    )
            if not by_block:
                break
            affected = [block for block in paragraphs if block.id in by_block]
            repair_context = {
                block.id: {
                    "section": section.title,
                    "preceding": section.blocks[index - 1].text if index else "",
                    "following": section.blocks[index + 1].text if index + 1 < len(section.blocks) else "",
                }
                for section in document.sections for index, block in enumerate(section.blocks)
                if block.id in by_block
            }
            with ThreadPoolExecutor(max_workers=self._parallel_workers(len(affected))) as executor:
                replacements = list(executor.map(lambda b: self._repair_block(b, lookup, by_block[b.id], article_context=repair_context[b.id]), affected))
            changed_count = 0
            for block, replacement in zip(affected, replacements):
                if replacement and replacement != block.text:
                    block.text = replacement
                    repairs += 1
                    changed_count += 1
            if not changed_count:
                logger.warning(
                    "Automatic repair made no changes in round %d/%d; retrying within the configured repair budget",
                    attempt + 1,
                    rounds,
                )
            prior_round_made_no_progress = changed_count == 0
        metrics = self._quality_metrics(document, paper_ir, ledger)
        metrics.update(article_context_verified=context_verified, article_global_verified=global_verified, replaced_paragraph_count=repairs)
        report = QualityReport(passed=False, mode="failed", score=0.0, metrics=metrics, issues=issues)
        raise ArticleQualityError(document, report)

    def _review_global_consistency(self, document: ArticleDocument, ledger: EvidenceLedger, model: str | None) -> list[QualityIssueIR] | None:
        paragraphs = [{"block_id": b.id, "section": s.title, "text": b.text, "claim_ids": b.claim_ids}
            for s in document.sections for b in s.blocks if b.type == "paragraph"]
        valid_ids = {b["block_id"] for b in paragraphs}
        used_claims = {cid for b in paragraphs for cid in b["claim_ids"]}
        sources = {e.block_id: {"source_id": e.block_id, "page": e.page, "text": e.quote}
            for claim in ledger.claims if claim.id in used_claims for e in claim.evidence}
        checks = {"attribution", "scope", "numerics", "conclusions", "proof_attribution", "discourse"}
        for attempt in range(2):
            response = self._llm.generate_json(
                system_prompt=(
                    "你是独立的论文结论审计员，专门发现逐段核验容易漏掉的跨章节矛盾。以原文为唯一依据，旧 claims 不是事实权威。"
                    "PDF 表格文本可能跨行错位；出现 Original TeX 表格时，必须按 & 列和 \\\\ 行绑定策略与指标，禁止根据扁平PDF文本的先后顺序归属数字。"
                    "依次检查：1. attribution：区分本文新结果与引言引用的既有研究，原文 They/prior work/precursors 的结果不能写成本文证明；"
                    "同一论文内也须逐项核对证明归属：各构造、辅助过程和极限步骤属于哪个章节与定理，不能把相邻页面或相似符号的不同证明拼接。"
                    "2. scope：单因子与多因子、条件于随机变量与无条件、固定样本与样本增长、可估计下界与可估计总量、可估计与可消除不得混淆；"
                    "理论文章必须区分有限期限、无限期限、生成元截断与期限趋于无穷，逐个核对取极限的指标和保持固定的参数。"
                    "章节标题也是断言的语境；把有限期限证明放在无限期限章节却未说明其原归属，或用其中的辅助变量解释另一构造，应判错。"
                    "此后、据此、这一构造等连接词必须对应同一证明的真实依赖或原文章节顺序，不可仅凭各句单独有出处判为正确。"
                    "同时核对公式与文字定义：分布的rate与scale参数、比率分子分母、买卖方向和正负号不能互换。"
                    "3. numerics：原文正文与表格冲突必须明确说明来源差异，禁止任意选一个值或替作者猜测解释；"
                    "4. conclusions：最后的概括、意义和局限不得扩大正文结论；若一处说某量不可估计，另一处又声称存在其一致估计量，必须说明不同条件或判错。"
                    "5. proof_attribution：单独核对每个证明段落中的构造、辅助过程与结论归属；固定期限、无限期限、逆问题和唯一性证明不得交叉嫁接。"
                    "6. discourse：逐段解析上述、此后、据此、这一构造等的指代对象；要求当前成稿中确有先行内容，且与原文的证明顺序和逻辑一致。"
                    "修复后的文章不应提及旧稿错误、审校意见或读者看不到的已删除论述。指出有问题的连接词及实际先行内容，不因句子各自真实而忽略错误连接。"
                    "只判断实际陈述，不要求穷尽所有细节。每个错误给出具体 block_id、冲突原句与原文限定条件，供自动修复。"
                    "issues 只能包含最终确认的错误，绝不输出思考过程、反问、自我纠正或‘该段无错误’等正确性说明。"
                    "若已分别标注正文值和表格值，指出原文不一致，则该项通过，不要求作者之外的人消除该矛盾。只返回最终 JSON。"
                ),
                user_prompt=(
                    '返回 {"checks":{"attribution":true,"scope":true,"numerics":true,"conclusions":true,"proof_attribution":true,"discourse":true},'
                    '"issues":[]}。有实际错误时将对应 checks 值设为 false，'
                    '并在 issues 加入 {"block_id":"错误段落ID","message":"具体错误和修正依据"}。'
                    "每个 checks 值必须是布尔值；有错误的项目为 false，issues 仅含有错段落；全部通过时 issues 为空数组。\n"
                    + ("上次返回不是一致的最终审计结论。请重新判断；正确的段落不要放进 issues。\n" if attempt else "") +
                    f"完整文章段落：{json.dumps(paragraphs, ensure_ascii=False)}\n"
                    f"对应原文及相邻上下文：{json.dumps(list(sources.values()), ensure_ascii=False)}"
                ), model=model, reasoning_budget=8192,
            )
            flags = response.get("checks") if isinstance(response, dict) else None
            rows = response.get("issues") if isinstance(response, dict) else None
            if not isinstance(flags, dict) or set(flags) != checks or any(type(v) is not bool for v in flags.values()) or not isinstance(rows, list):
                continue
            if any(not isinstance(row, dict) or row.get("block_id") not in valid_ids or not str(row.get("message", "")).strip() for row in rows):
                continue
            if any(re.search(r"(?:该段|段落|本身|陈述).{0,12}(?:无实际错误|无错误|没有错误)|supported\s*(?:应为|为|=)\s*true|段落处理正确", str(row["message"]), re.I) for row in rows):
                logger.warning("Global audit contained self-contradictory issue explanations; requesting a final verdict")
                continue
            if all(flags.values()) and not rows:
                reader_blocks = [{"block_id": b.id, "section": s.title, "type": b.type, "text": b.text}
                    for s in document.sections for b in s.blocks if b.text]
                return self._review_reader_continuity(reader_blocks, model)
            if not all(flags.values()) and rows:
                return [QualityIssueIR(code="article_global_mismatch", block_id=row["block_id"], message=str(row["message"])[:1800]) for row in rows]
            logger.warning("Inconsistent global audit flags on attempt %d", attempt + 1)
        return None

    def _review_reader_continuity(self, paragraphs: list[dict], model: str | None) -> list[QualityIssueIR] | None:
        valid_ids = {paragraph["block_id"] for paragraph in paragraphs if paragraph.get("type", "paragraph") == "paragraph"}
        for attempt in range(2):
            response = self._llm.generate_json(
                system_prompt=(
                    "你是最终中文成稿的独立读者审校员。只检查本文实际可读性与逻辑衔接，不推测论文原文，不重审数学定理正确性。"
                    "逐段核对上述、前文、下文、此后、据此、这一构造等连接词：实际先行段落是否提过该对象，是否从一个证明跳到另一证明却写成同一构造。"
                    "禁止旧稿修复说明、无先行对象的上述、指向不存在章节的前后引用。修复后的正文应直接介绍内容，不能让读者猜测被删掉的旧句。"
                    "假设、定理、方程和图表的编号默认是对原论文的引用，可以按解读需要重排，不要求先定义再引用或编号递增。"
                    "仅显式声称本文前后段落或章节已有某内容时，才要求成稿中有对应先行内容；不要把原论文编号误当作本文编号。"
                    "术语或符号可以在使用它的同一句中介绍，例如‘估计主方向h’已经说明h的含义，不要求更早定义。显示公式也是成稿的一部分。"
                    "不以符号解释不够详细、未展开全部假设或缺少教学背景判错；这里只查错误指代和逻辑衔接。问题block_id只能指向paragraph类型的正文段落。"
                    "只报告实际存在的错误并引用原句，正常概述或术语重复不是错误；不以个人文风偏好、未穷尽细节或未提供原论文为由判错。"
                    "返回严格 JSON：{\"passed\":true,\"issues\":[]}；有错时 passed=false，issues 为 [{\"block_id\":\"...\",\"message\":\"错误原句、缺失或错配的先行对象及修正要求\"}]。"
                ),
                user_prompt=(
                    ("上次返回结构不完整或标记互相矛盾，请给出完整最终结论。\n" if attempt else "")
                    + "以下是读者看到的全部成稿，按实际章节和段落顺序排列：\n"
                    + json.dumps(paragraphs, ensure_ascii=False)
                ),
                model=model, reasoning_budget=4096,
            )
            if not isinstance(response, dict) or type(response.get("passed")) is not bool:
                continue
            rows = response.get("issues")
            if not isinstance(rows, list) or any(
                not isinstance(row, dict) or row.get("block_id") not in valid_ids
                or not str(row.get("message", "")).strip() for row in rows
            ):
                continue
            if response["passed"] and not rows:
                return []
            if not response["passed"] and rows:
                return [QualityIssueIR(code="article_global_mismatch", block_id=row["block_id"],
                    message=str(row["message"])[:1800]) for row in rows]
        return None

    @staticmethod
    def _stabilize_verified_document(
        document: ArticleDocument,
        ledger: EvidenceLedger,
        *,
        min_paragraphs: int,
        min_chars: int,
    ) -> None:
        """Keep evidence-only repairs from making a verified article too short.

        A repaired paragraph is intentionally conservative: it is replaced with
        verified claim text. That can remove useful prose and leave the article
        just below the structural gate. This helper adds previously uncovered,
        verified claims as standalone paragraphs and removes exact duplicates.
        Every added paragraph remains provenance-bound, so this is not a prose
        fallback or a relaxation of the verification gate.
        """
        seen: set[str] = set()
        for section in document.sections:
            kept: list[ArticleBlockIR] = []
            for block in section.blocks:
                if block.type != "paragraph":
                    kept.append(block)
                    continue
                normalized = _normalize_text(block.text)
                if normalized and normalized in seen:
                    continue
                if normalized:
                    seen.add(normalized)
                kept.append(block)
            section.blocks = kept

        paragraphs = [
            block
            for section in document.sections
            for block in section.blocks
            if block.type == "paragraph"
        ]
        covered = {claim_id for block in paragraphs for claim_id in block.claim_ids}
        existing = {_normalize_text(block.text) for block in paragraphs if block.text.strip()}
        candidates = [
            claim
            for claim in ledger.claims
            if claim.verified
            and claim.claim_type != "future_work"
            and _normalize_text(claim.claim) not in existing
        ]
        target_chars = max(min_chars, min_chars + 300)
        index = 1
        while candidates and (len(paragraphs) < max(1, min_paragraphs) or sum(len(block.text.strip()) for block in paragraphs) < target_chars):
            claim = candidates.pop(0)
            normalized = _normalize_text(claim.claim)
            if not normalized or normalized in existing:
                continue
            section = PaperUnderstandingPipeline._section_for_claim(document, claim.claim_type)
            section.blocks.append(
                ArticleBlockIR(
                    id=f"block-verified-supplement-{index:03d}",
                    type="paragraph",
                    text=claim.claim.strip(),
                    claim_ids=[claim.id],
                )
            )
            index += 1
            paragraphs.append(section.blocks[-1])
            covered.add(claim.id)
            existing.add(normalized)

    @staticmethod
    def _section_for_claim(document: ArticleDocument, claim_type: str) -> ArticleSectionIR:
        keywords = {
            "method": ("方法", "模型", "框架", "机制", "设定"),
            "assumption": ("假设", "设定", "条件", "模型"),
            "data": ("数据", "样本", "实证", "模拟"),
            "empirical_result": ("结果", "实证", "模拟", "验证"),
            "numeric_result": ("结果", "实证", "模拟", "验证"),
            "limitation": ("限制", "讨论", "结论"),
            "conclusion": ("结论", "讨论", "意义"),
        }
        wanted = keywords.get(claim_type, ())
        for section in document.sections:
            if wanted and any(word in section.title for word in wanted):
                return section
        return min(document.sections, key=lambda section: len(section.blocks))

    def _review_article_context(self, document: ArticleDocument, paper_ir: PaperIR, model: str | None, ledger: EvidenceLedger | None = None) -> dict[str, tuple[bool, list[str]]] | None:
        paragraphs = [block for section in document.sections for block in section.blocks if block.type == "paragraph"]
        if ledger is not None:
            batches = [paragraphs[index:index + 5] for index in range(0, len(paragraphs), 5)]
            def review_batch(batch):
                bound = {cid for block in batch for cid in block.claim_ids}
                source = {e.block_id: {"page": e.page, "text": e.quote} for c in ledger.claims if c.id in bound for e in c.evidence}
                outline = [
                    {"section": section.title, "text": block.text}
                    for section in document.sections
                    for block in section.blocks
                    if block.type == "paragraph"
                ]
                expected = {b.id for b in batch}
                displays = [{"section": s.title, "after_claim_ids": b.claim_ids, "latex": b.text}
                    for s in document.sections for b in s.blocks if b.type == "equation" and bound.intersection(b.claim_ids)]
                for attempt in range(2):
                    response = self._llm.generate_json(
                        system_prompt=("你是严格的论文全文审校员。基于原文检查指定段落在全文中的事实、条件、增减方向与前后矛盾。"
                            "PDF 表格文本可能错位，Original TeX 中 & 分列、\\\\ 分行的原始行列对应优先于PDF文本阅读顺序。"
                            "原文优先于旧声明；检查模拟与实证、固定与增长样本、随机与确定极限。"
                            "理论证明须核对所属章节、定理、有限或无限期限、取极限的指标与固定参数，不能因符号相似把不同证明拼成因果链。"
                            "结合当前章节标题判断段落的适用范围；跨定理引用必须明确原归属，此后、据此、这一构造等指代必须有对应依据。"
                            "若正文与表格有冲突，文章分别说明来源和值即为正确，不可要求选定某一值或编造原因。"
                            "正常的引言概述不算重复，但无新信息的整段复述应指出。仅判断文中实际断言，不因省略无关细节判错。"
                            "同时核对段落所解释的显示公式与原文是否一致，变量含义和公式前后的文字不得矛盾。"
                            "分布的rate与scale、比率的分子分母必须与原文定义一致；指向上一节或下一节时必须符合当前文章顺序。"
                            "只返回指定段落的 JSON verdicts；issues 仅列出最终确认的实际错误，每段最多两条。"
                            "supported=true 时 issues 必须为空；supported=false 时必须列出错误，禁止在 issues 输出思考过程或无错误说明。"),
                        user_prompt=('返回 {"verdicts":[{"block_id":"...","supported":true,"issues":[]}]}。\n'
                            f"必须恰好包含这些 block_id：{json.dumps(sorted(expected))}。supported 必须为布尔值。\n"
                            + ("上次返回结构无效；必须返回完整 verdicts 数组，不能返回空对象、遗漏段落或字符串布尔值。\n" if attempt else "")
                            +
                            f"当前段落的原文证据：{json.dumps(list(source.values()), ensure_ascii=False)}\n"
                            f"关联的显示公式：{json.dumps(displays, ensure_ascii=False)}\n"
                            f"全文段落提纲（仅用于检查前后矛盾）：{json.dumps(outline, ensure_ascii=False)}\n"
                            f"此次只审校段落：{json.dumps([{'block_id': b.id, 'text': b.text} for b in batch], ensure_ascii=False)}"), model=model, reasoning_budget=4096)
                    rows = response.get("verdicts") if isinstance(response, dict) else None
                    if _valid_block_verdict_rows(rows):
                        result = {str(item.get("block_id")): (item["supported"], [str(x) for x in item["issues"]]) for item in rows}
                        if set(result) == expected and len(rows) == len(expected):
                            return result
                        logger.warning("Context verdict IDs mismatch (attempt %d/2): expected=%s received=%s", attempt + 1, sorted(expected), sorted(result))
                    else:
                        logger.warning("Invalid context verdict schema (attempt %d/2): blocks=%s", attempt + 1, sorted(expected))
                if len(batch) > 1:
                    midpoint = len(batch) // 2
                    logger.warning("Splitting context review after invalid responses: %d paragraphs", len(batch))
                    smaller = [review_batch(batch[:midpoint]), review_batch(batch[midpoint:])]
                    if all(result is not None for result in smaller):
                        return {key: value for result in smaller for key, value in result.items()}
                return None
            with ThreadPoolExecutor(max_workers=self._parallel_workers(len(batches))) as executor:
                results = list(executor.map(review_batch, batches))
            if any(result is None for result in results):
                return None
            return {key: value for result in results for key, value in result.items()}
        source = [{"page": block.page, "text": block.text} for block in _balanced_source_blocks(paper_ir, 30000)]
        response = self._llm.generate_json(
            system_prompt=(
                "你是严格的论文全文审校员。依据原文检查文章的跨段落一致性，而不是只看句子是否通顺。"
                "逐一核对样本量和维度的极限条件、随机极限与确定值、增加/减少方向、因果与相关、模拟与真实实证。"
                "不能把固定样本下不可估计推广成增加样本量也无法消除，不能把某一组模拟结论推广到所有市场。"
                "检查正文是否前后矛盾，是否多个章节换说法反复叙述同一结论却没有新信息。"
                "任一断言无原文依据即判该段 supported=false，并准确指出删除或修改什么及原文依据。只返回 JSON。"
            ),
            user_prompt=(
                '对每个段落返回 {"verdicts":[{"block_id":"...","supported":true,"issues":[]}]}。\n'
                f"原文：{json.dumps(source, ensure_ascii=False)}\n"
                f"整篇解读：{document.model_dump_json()}"
            ),
            model=model,
        )
        if not response or not isinstance(response.get("verdicts"), list):
            return None
        verdicts = {}
        for item in response["verdicts"]:
            if not isinstance(item, dict):
                continue
            block_id = str(item.get("block_id") or "")
            issues = item.get("issues")
            verdicts[block_id] = (item.get("supported") is True, [str(issue) for issue in issues] if isinstance(issues, list) else [])
        return verdicts if set(verdicts) == {block.id for block in paragraphs} else None

    def _verify_article_blocks(
        self,
        blocks: list[ArticleBlockIR],
        claim_lookup: dict[str, EvidenceClaimIR],
        model: str | None,
    ) -> dict[str, tuple[bool, list[str]]] | None:
        if len(blocks) > 5:
            batches = [blocks[index:index + 5] for index in range(0, len(blocks), 5)]
            with ThreadPoolExecutor(max_workers=self._parallel_workers(len(batches))) as executor:
                results = list(executor.map(lambda batch: self._verify_article_blocks(batch, claim_lookup, model), batches))
            if any(result is None for result in results):
                return None
            return {key: value for result in results for key, value in result.items()}
        payload = []
        structured_source = {}
        for block in blocks:
            claims = [claim_lookup[claim_id] for claim_id in block.claim_ids if claim_id in claim_lookup]
            structured_source.update({e.block_id: e.quote for claim in claims for e in claim.evidence if e.block_id.startswith("tex-")})
            payload.append(
                {
                    "block_id": block.id,
                    "text": block.text,
                    "claims": [
                        {
                            "claim": claim.claim,
                        }
                        for claim in claims
                    ],
                    "evidence": list(dict.fromkeys(evidence.quote for claim in claims for evidence in claim.evidence if not evidence.block_id.startswith("tex-"))),
                }
            )
        expected = {block.id for block in blocks}
        for attempt in range(2):
            response = self._llm.generate_json(
                system_prompt=(
                    "你是独立文章事实核验员。每个段落只能表达绑定 claims 与 evidence 完全支持的内容。"
                    "PDF表格文本可能跨行错位；若有Original TeX表格，必须使用其 & 列和 \\\\ 行绑定数据对象。"
                    "待核验正文仅为每项的 text 字段；claims 是参考资料，不能把 claims 中的句子误当成正文。"
                    "不要求正文逐条复述 claims；若 text 没有声称某件事，不得以该声称为由判错。每条问题必须引用 text 中实际存在的原句。"
                    "任何无原文依据的事实、数字、因果、比较级或过度推断都判为 unsupported。原文优先于旧声明。只返回 JSON。"
                    "正确但未穷尽所有细节的段落不是错误；不要因未涵盖无关细节判为 false。"
                    "特别核对量词、渐近条件、增减方向、随机与确定性、模拟与实证的区别；正确结论不能证明错误条件。"
                    "既有研究的结果不可写成本文贡献；原文内部冲突须明确标注不同来源，不可任选一个数据掩盖冲突。"
                    "如果 text 已分别标明正文值和表格值并指出不一致，必须按其事实转述判断，不能以未解决原论文矛盾判错。"
                    "issues 仅列出最终确认的实际错误；supported=true 时 issues 必须为空，supported=false 时必须列出具体错误。"
                    "不得在 issues 输出思考过程、自我纠正或无错误说明。"
                ),
                user_prompt=(
                    '返回 {"verdicts":[{"block_id":"...","supported":true,"issues":[]}]}。\n'
                    f"必须恰好包含 {json.dumps(sorted(expected))} 中的每个 ID，supported 为布尔值，issues 为数组。\n"
                    f"原始TeX结构化证据：{json.dumps(list(structured_source.values()), ensure_ascii=False)}\n"
                    f"内容：{json.dumps(payload, ensure_ascii=False)}"
                ), model=model, reasoning_budget=4096,
            )
            rows = response.get("verdicts") if isinstance(response, dict) else None
            if _valid_block_verdict_rows(rows):
                verdicts = {str(row.get("block_id")): (row["supported"], [str(value) for value in row["issues"]]) for row in rows}
                if set(verdicts) == expected and len(rows) == len(expected):
                    return verdicts
            logger.warning("Invalid paragraph verdict batch on attempt %d: expected=%s response=%s", attempt + 1, sorted(expected), json.dumps(response, ensure_ascii=False)[:1800])
        if len(blocks) > 1:
            midpoint = len(blocks) // 2
            smaller = [self._verify_article_blocks(batch, claim_lookup, model) for batch in (blocks[:midpoint], blocks[midpoint:])]
            if all(result is not None for result in smaller):
                return {key: value for result in smaller for key, value in result.items()}
        return None

    def _repair_block(
        self,
        block: ArticleBlockIR,
        claim_lookup: dict[str, EvidenceClaimIR],
        issues: list[str],
        *,
        article_context: dict[str, str] | None = None,
    ) -> str | None:
        claims = [claim_lookup[claim_id] for claim_id in block.claim_ids if claim_id in claim_lookup]
        numeric_evidence = " ".join([claim.claim for claim in claims] + [item.quote for claim in claims for item in claim.evidence])
        allowed_numbers = sorted(set(_numeric_tokens(numeric_evidence)))
        response = self._llm.generate_json(
            system_prompt=(
                "你是论文段落修复器。删除所有无证据内容，只用提供的 claims 和 evidence 重写一个中文段落。"
                "审校意见也可能读错PDF表格；原始TeX中 & 分列、\\\\ 分行，必须按这些行列对应归属数据，不得复述审校意见中的错配数字。"
                "原文 evidence 优先于旧 claims，不能重复旧声明中的错误。保持段落信息量，补足原文明示的条件与解释，不添加建议、未来方向或常识。只返回 JSON。"
                "删除正文中的 claim-001、eq-002、p-006-0084 等内部标识，使用原文页码和图表编号，保留有证据的变量解释，不添加 Markdown 标题或显示公式。"
                "正文优先用中文解释变量；必要的行内数学必须用 $...$ 包裹，不能直接输出 x_i、R^{p} 或裸露的 LaTeX 命令。"
                "保留前人研究与本文贡献的区别及单因子、固定样本等适用条件；遇原文内部冲突，应注明来源差异，不可猜测原因或改选另一数字掩盖矛盾。"
                "理论段落必须保留证明的章节与定理归属，区分有限或无限期限、生成元截断与期限极限；相似符号不能作为混合不同构造的依据。"
                "跨证明引用须明确说明其原归属，删除没有原文依据的此后、据此、这一构造等因果或顺序连接。"
                "直接写最终读者应看到的正文，不要解释旧稿哪里写错、复述审校意见或引用已经删除的内容。"
                "上述、这一构造等指代必须在提供的当前相邻内容中有真实先行对象，否则直接写明对象。"
            ),
            user_prompt=(
                '返回 {"text":""}，text 必须填入完整的实际修复正文，禁止照抄字段示例或返回占位符。\n'
                f"文章章节与相邻内容（仅用于连贯性和适用范围，不能作为原文证据）：{json.dumps(article_context or {}, ensure_ascii=False)}\n"
                f"原段落：{block.text}\n问题：{json.dumps(issues, ensure_ascii=False)}\n"
                f"数字白名单（含单位后缀，必须逐字匹配）：{json.dumps(allowed_numbers, ensure_ascii=False)}\n"
                "修复数字问题时，白名单未包含的数值必须从正文删除；不能因其他原文表格出现过就保留，"
                "也不能自行添加百分号或推算替代数字。可用已核验的定性比较保留结论，并保留必要的适用条件。\n"
                f"声明：{json.dumps([{'id': c.id, 'claim': c.claim} for c in claims], ensure_ascii=False)}\n"
                f"证据：{json.dumps(list({e.block_id: e.model_dump() for c in claims for e in c.evidence}.values()), ensure_ascii=False)}"
            ),
            model=self._article_model,
            reasoning_budget=4096,
        )
        if not response:
            return None
        value = response.get("text")
        if not value and set(response) == {"paragraph"}:
            value = response["paragraph"]
        text = re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""
        if len(text) < 20 or text in {"修复后的段落", "中文段落", "修复后的正文"}:
            logger.warning("Rejected placeholder or empty paragraph repair for %s: %s", block.id, json.dumps(response, ensure_ascii=False)[:600])
            return None
        return _normalize_inline_math(text[:2400]) if text else None

    @staticmethod
    def _deterministic_issues(
        document: ArticleDocument,
        paper_ir: PaperIR,
        ledger: EvidenceLedger,
        *,
        enforce_chinese: bool = True,
        strict_quality: bool = False,
        min_paragraphs: int = 8,
        min_sections: int = 4,
        min_chars: int = 1800,
    ) -> list[QualityIssueIR]:
        issues: list[QualityIssueIR] = []
        claim_lookup = {claim.id: claim for claim in ledger.claims}
        selected_figures = {figure.id for figure in paper_ir.figures if figure.selected}
        used_figures: list[str] = []
        used_equations: set[str] = set()
        future_allowed = any(_is_explicit_future_claim(claim) for claim in ledger.claims)
        for section in document.sections:
            if not future_allowed and re.search(r"未来|展望|下一步|future", section.title, re.I):
                issues.append(QualityIssueIR(code="unsupported_future_section", message=f"Unsupported future-work section: {section.title}"))
            for block in section.blocks:
                dangling_reference = any(
                    not re.search(r"原(?:文|论文).{0,6}$", block.text[max(0, match.start() - 12):match.start()])
                    for match in re.finditer(r"下一节|下节", block.text)
                )
                if block.type == "paragraph" and section is document.sections[-1] and dangling_reference:
                    issues.append(QualityIssueIR(code="dangling_section_reference", message="本文已到最后一节，不能说下一节讨论。请按当前文章顺序改写；若指原论文，应明确标注原文的章节。", block_id=block.id))
                if block.type == "figure":
                    if block.asset_id:
                        used_figures.append(block.asset_id)
                    continue
                if block.type == "equation":
                    math_error = malformed_math_reason(block.text, formula_only=True)
                    if math_error:
                        issues.append(QualityIssueIR(code="corrupted_math", message=math_error, block_id=block.id))
                    equation_key = re.sub(r"\s+", "", block.text)
                    if equation_key in used_equations:
                        issues.append(QualityIssueIR(code="duplicate_equation", message="同一公式在文章中重复展示。", block_id=block.id))
                    used_equations.add(equation_key)
                    if not _balanced_latex(block.text):
                        issues.append(QualityIssueIR(code="invalid_equation", message="Equation has unbalanced LaTeX braces.", block_id=block.id))
                    continue
                if not block.claim_ids or any(claim_id not in claim_lookup for claim_id in block.claim_ids):
                    issues.append(QualityIssueIR(code="missing_provenance", message="Paragraph has missing or invalid claim IDs.", block_id=block.id))
                    continue
                if enforce_chinese and len(block.text) >= 40 and _cjk_ratio(block.text) < (0.20 if strict_quality else 0.08):
                    issues.append(
                        QualityIssueIR(
                            code="non_chinese_paragraph",
                            message="Chinese article contains a predominantly non-Chinese paragraph.",
                            block_id=block.id,
                        )
                    )
                evidence_text = " ".join(
                    [claim_lookup[claim_id].claim for claim_id in block.claim_ids]
                    + [
                        evidence.quote
                        for claim_id in block.claim_ids
                        for evidence in claim_lookup[claim_id].evidence
                    ]
                )
                supported_numbers = set(_numeric_tokens(evidence_text))
                reader_text = re.sub(r"(?:原文)?第\s*(\d+)\s*页", lambda match: "原文页码" if any(e.page == int(match.group(1)) for cid in block.claim_ids for e in claim_lookup[cid].evidence) else match.group(), block.text)
                unsupported_numbers = [token for token in _numeric_tokens(reader_text) if token not in supported_numbers]
                if unsupported_numbers:
                    issues.append(
                        QualityIssueIR(
                            code="unsupported_number",
                            message=f"Paragraph contains numbers absent from its evidence: {unsupported_numbers[:5]}",
                            block_id=block.id,
                        )
                    )
        if len(used_figures) != len(set(used_figures)):
            issues.append(QualityIssueIR(code="duplicate_figure", message="A figure is inserted more than once."))
        if strict_quality and any(
            figure.id in used_figures and not (figure.analysis.vision_verified and figure.analysis.caption_verified)
            for figure in paper_ir.figures
        ):
            issues.append(QualityIssueIR(code="unverified_figure_caption", message="入选图片及中文图注必须完成独立核验。"))
        if set(used_figures) != selected_figures:
            issues.append(
                QualityIssueIR(
                    code="figure_placement_mismatch",
                    message="Selected figures and inserted figures do not match.",
                )
            )
        paragraphs = [
            block
            for section in document.sections
            for block in section.blocks
            if block.type == "paragraph"
        ]
        if strict_quality:
            if _cjk_ratio(document.title) < 0.15:
                issues.append(QualityIssueIR(code="non_chinese_title", message="论文解读标题必须以中文为主。"))
            section_count = sum(1 for section in document.sections if section.blocks)
            char_count = sum(len(block.text.strip()) for block in paragraphs)
            if len(paragraphs) < max(1, min_paragraphs):
                issues.append(
                    QualityIssueIR(
                        code="article_too_short",
                        message=f"文章段落数不足：{len(paragraphs)}，至少需要 {min_paragraphs}。",
                    )
                )
            if section_count < max(1, min_sections):
                issues.append(
                    QualityIssueIR(
                        code="article_section_coverage",
                        message=f"文章有效章节不足：{section_count}，至少需要 {min_sections}。",
                    )
                )
            if char_count < max(1, min_chars):
                issues.append(
                    QualityIssueIR(
                        code="article_char_count",
                        message=f"文章正文过短：{char_count} 字符，至少需要 {min_chars}。",
                    )
                )
            normalized_blocks = [_normalize_text(block.text) for block in paragraphs if block.text.strip()]
            if len(normalized_blocks) != len(set(normalized_blocks)):
                issues.append(QualityIssueIR(code="duplicate_paragraph", message="文章包含重复段落。"))
            raw_source_markers = ("```", "$$", "\\begin{", "\\end{", "![", "[AUTOSCI_IMG_")
            for block in paragraphs:
                math_error = malformed_math_reason(block.text)
                if math_error:
                    issues.append(QualityIssueIR(code="corrupted_math", message=math_error + "; 依据原文重新输出完整数学表达式，JSON中的反斜杠必须正确转义，不能丢失命令。", block_id=block.id))
                if len(block.text.strip()) < 12 or block.text.strip() in {"修复后的段落", "中文段落", "修复后的正文"} or re.search(r"某估计|某些范数", block.text):
                    issues.append(QualityIssueIR(code="placeholder_paragraph", message="段落含占位符或不明确的估计对象，请依据原文说明具体对象、条件和结论；不能以‘某估计’或‘某些范数’代替解释。", block_id=block.id))
                if re.search(r"\b(?:claim|eq|equation|block|article-sec)-\d", block.text):
                    issues.append(QualityIssueIR(code="internal_reference_residue", message="正文出现工作流内部 ID，请改为自然语言指代。", block_id=block.id))
                prose = re.sub(r"(?<!\\)\$[^$\n]+(?<!\\)\$|\\\([^\n]*?\\\)|\\\[[\s\S]*?\\\]", "", block.text)
                bare_math = re.search(r"[A-Za-z\u0391-\u03c9\u211d\u2220][_^](?:\{[^{}]*\}|[A-Za-z0-9\u0391-\u03c9])|\\[A-Za-z]+(?:\{|\b)|[_^](?:\{|[A-Za-z0-9\u0391-\u03c9])", prose)
                if bare_math:
                    issues.append(QualityIssueIR(
                        code="unrendered_inline_math",
                        message=f"正文包含未渲染数学源码 {bare_math.group()!r}；请改为中文变量说明，或使用 $...$ 包裹完整的行内公式。",
                        block_id=block.id,
                    ))
                if any(marker in block.text for marker in raw_source_markers):
                    issues.append(
                        QualityIssueIR(
                            code="raw_markup_residue",
                            message="正文段落仍含 Markdown/LaTeX 源码标记。",
                            block_id=block.id,
                        )
                    )
        return issues

    @staticmethod
    def _force_extractive_document(
        document: ArticleDocument,
        ledger: EvidenceLedger,
        paper_ir: PaperIR,
    ) -> ArticleDocument:
        claim_lookup = {claim.id: claim for claim in ledger.claims}
        valid_figures = {
            figure.id
            for figure in paper_ir.figures
            if figure.selected and figure.analysis.vision_verified and figure.related_claim_ids
        }
        for figure in paper_ir.figures:
            if figure.id not in valid_figures:
                figure.selected = False
        for section in document.sections:
            rebuilt: list[ArticleBlockIR] = []
            for block in section.blocks:
                if block.type == "figure":
                    if block.asset_id in valid_figures:
                        rebuilt.append(block)
                    continue
                if block.type == "equation":
                    if _balanced_latex(block.text):
                        rebuilt.append(block)
                    continue
                claims = [claim_lookup[claim_id] for claim_id in block.claim_ids if claim_id in claim_lookup]
                if not claims:
                    continue
                block.text = " ".join(claim.claim for claim in claims)
                rebuilt.append(block)
            section.blocks = rebuilt
        return document

    @staticmethod
    def _quality_metrics(document: ArticleDocument, paper_ir: PaperIR, ledger: EvidenceLedger) -> dict[str, float | int | bool]:
        paragraphs = [block for section in document.sections for block in section.blocks if block.type == "paragraph"]
        figures = [block for section in document.sections for block in section.blocks if block.type == "figure"]
        equations = [block for section in document.sections for block in section.blocks if block.type == "equation"]
        covered_claims = {claim_id for block in paragraphs for claim_id in block.claim_ids}
        return {
            "paragraph_count": len(paragraphs),
            "section_count": sum(bool(section.blocks) for section in document.sections),
            "body_char_count": sum(len(block.text.strip()) for block in paragraphs),
            "chinese_ratio": round(_cjk_ratio(" ".join(block.text for block in paragraphs)), 4),
            "claim_count": len(ledger.claims),
            "covered_claim_count": len(covered_claims),
            "claim_coverage": round(len(covered_claims) / max(1, len(ledger.claims)), 4),
            "figure_count": len(figures),
            "vision_verified_figure_count": sum(
                1 for figure in paper_ir.figures if figure.selected and figure.analysis.vision_verified
            ),
            "figure_captions_verified": all(
                figure.analysis.caption_verified for figure in paper_ir.figures if figure.selected
            ),
            "equation_count": len(equations),
            "all_paragraphs_have_provenance": all(bool(block.claim_ids) for block in paragraphs),
        }

    @staticmethod
    def _render_markdown(document: ArticleDocument, paper_ir: PaperIR) -> str:
        figure_lookup = {figure.id: figure for figure in paper_ir.figures}
        lines = [f"# {document.title}", ""]
        authors = ", ".join(document.authors) if document.authors else "未知"
        lines.extend([f"- 作者：{authors}", f"- arXiv 链接：{document.arxiv_url}", ""])
        for section in document.sections:
            if not section.blocks:
                continue
            lines.extend([f"## {section.title}", ""])
            for block in section.blocks:
                if block.type == "paragraph":
                    lines.extend([escape_currency_dollars(PaperParser._expand_math_macros(block.text.strip(), paper_ir.math_macros)), ""])
                elif block.type == "equation":
                    lines.extend(["$$", PaperParser._expand_math_macros(block.text.strip(), paper_ir.math_macros), "$$", ""])
                elif block.type == "figure" and block.asset_id in figure_lookup:
                    figure = figure_lookup[block.asset_id]
                    alt = re.sub(r"[\[\]]", "", figure.source_label) or "论文图示"
                    caption_text = figure.analysis.summary if figure.analysis.vision_verified and figure.analysis.summary else figure.caption
                    caption = re.sub(r"\s+", " ", caption_text).strip()
                    lines.extend(
                        [
                            f"![{alt}]({figure.artifact_url})",
                            "",
                            f"*{figure.source_label}（原文第 {figure.page} 页）：{caption}*",
                            "",
                        ]
                    )
        return "\n".join(lines).strip() + "\n"

    @staticmethod
    def _save_artifacts(
        paper_dir: Path,
        paper_ir: PaperIR,
        ledger: EvidenceLedger,
        plan: ArticlePlan,
        document: ArticleDocument,
        report: QualityReport,
        markdown: str,
    ) -> dict[str, Path]:
        paths = {
            "paper_ir": paper_dir / "paper_ir.json",
            "evidence_ledger": paper_dir / "evidence_ledger.json",
            "article_plan": paper_dir / "article_plan.json",
            "article_document": paper_dir / "article_document.json",
            "quality_report": paper_dir / "qa_report.json",
            "markdown": paper_dir / "note.md",
        }
        paths["paper_ir"].write_text(paper_ir.model_dump_json(indent=2), encoding="utf-8")
        paths["evidence_ledger"].write_text(ledger.model_dump_json(indent=2), encoding="utf-8")
        paths["article_plan"].write_text(plan.model_dump_json(indent=2), encoding="utf-8")
        paths["article_document"].write_text(document.model_dump_json(indent=2), encoding="utf-8")
        paths["quality_report"].write_text(report.model_dump_json(indent=2), encoding="utf-8")
        paths["markdown"].write_text(markdown, encoding="utf-8")
        return paths

    def _artifact_url(self, path: Path) -> str:
        resolved = path.resolve()
        root = Path(self._settings.artifacts_dir).resolve()
        try:
            rel = resolved.relative_to(root)
            return f"/artifacts/{rel.as_posix()}"
        except ValueError:
            return resolved.as_posix()


def _chunk_blocks(blocks: list[TextBlockIR], *, max_chars: int, max_items: int) -> Iterable[list[TextBlockIR]]:
    chunk: list[TextBlockIR] = []
    size = 0
    for block in blocks:
        if chunk and (len(chunk) >= max_items or size + len(block.text) > max_chars):
            yield chunk
            chunk = []
            size = 0
        chunk.append(block)
        size += len(block.text)
    if chunk:
        yield chunk


def _balanced_source_blocks(paper_ir: PaperIR, max_chars: int) -> list[TextBlockIR]:
    section_order = [section.id for section in paper_ir.sections]
    section_titles = {section.id: section.title.lower() for section in paper_ir.sections}
    grouped: dict[str, list[TextBlockIR]] = defaultdict(list)
    abstract: list[TextBlockIR] = []
    for block in paper_ir.blocks:
        if block.kind not in {"abstract", "paragraph"} or len(block.text) < 30:
            continue
        if block.kind == "abstract":
            abstract.append(block)
        else:
            grouped[block.section_id].append(block)

    selected: list[TextBlockIR] = []
    consumed = 0
    for block in abstract[:1]:
        selected.append(block)
        consumed += len(block.text)

    active_sections = [
        section_id
        for section_id in section_order
        if grouped.get(section_id)
        and not re.search(r"references|bibliography|参考文献", section_titles.get(section_id, ""), re.I)
    ]
    offsets = {section_id: 0 for section_id in active_sections}
    while active_sections and consumed < max_chars:
        progressed = False
        for section_id in list(active_sections):
            index = offsets[section_id]
            values = grouped[section_id]
            if index >= min(8, len(values)):
                active_sections.remove(section_id)
                continue
            block = values[index]
            offsets[section_id] += 1
            remaining = max_chars - consumed
            if remaining < 200:
                return selected
            if len(block.text) > remaining:
                block = block.model_copy(update={"text": block.text[:remaining]})
            selected.append(block)
            consumed += len(block.text)
            progressed = True
            if consumed >= max_chars:
                break
        if not progressed:
            break
    return selected


def _limit_claims(claims: list[EvidenceClaimIR], max_claims: int) -> list[EvidenceClaimIR]:
    if len(claims) <= max_claims:
        return claims
    caps = {
        "research_question": 3,
        "contribution": 5,
        "method": 9,
        "data": 6,
        "assumption": 3,
        "empirical_result": 10,
        "numeric_result": 6,
        "limitation": 4,
        "future_work": 3,
        "conclusion": 5,
    }
    selected: list[EvidenceClaimIR] = []
    counts: dict[str, int] = defaultdict(int)
    for claim in claims:
        if counts[claim.claim_type] >= caps.get(claim.claim_type, 3):
            continue
        selected.append(claim)
        counts[claim.claim_type] += 1
        if len(selected) >= max_claims:
            return selected
    if len(selected) < max_claims:
        selected_ids = {claim.id for claim in selected}
        selected.extend(claim for claim in claims if claim.id not in selected_ids)
    return selected[:max_claims]


def _sentences(text: str) -> list[str]:
    return [value.strip() for value in re.split(r"(?<=[.!?。！？])\s+", text) if len(value.strip()) >= 30]


def _normalize_text(text: str) -> str:
    return re.sub(r"\W+", "", text, flags=re.UNICODE).lower()


def _cjk_ratio(text: str) -> float:
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return 0.0
    cjk = sum("\u3400" <= char <= "\u9fff" for char in letters)
    return cjk / len(letters)


def _clamp_float(value: Any, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = fallback
    if math.isnan(number) or math.isinf(number):
        number = fallback
    return max(0.0, min(1.0, number))


def _valid_block_verdict_rows(rows: object) -> bool:
    if not isinstance(rows, list):
        return False
    return all(
        isinstance(row, dict)
        and type(row.get("supported")) is bool
        and isinstance(row.get("issues"), list)
        and all(isinstance(issue, str) and issue.strip() for issue in row["issues"])
        and row["supported"] == (not row["issues"])
        for row in rows
    )


def _normalize_inline_math(text: str) -> str:
    text = re.sub(r"\bp-(\d{3})-\d+\b", lambda match: f"原文第{int(match.group(1))}页", text)
    text = re.sub(r"(?P<bar>‖|\|\|)(?P<inside>[^‖\n]+?)(?P=bar)_\{(?P<sub>[^{}\n]+)\}",
        lambda match: match.group() if text[:match.start()].count("$") % 2 else "$" + r"\left\|" + match.group("inside").replace("$", "") + r"\right\|_{" + match.group("sub").replace("$", "") + "}$", text)
    text = re.sub(r"ˆE\[([^\]\n]+)\]", lambda match: "$" + r"\hat{E}[" + match.group(1).replace("$", "") + "]$", text)
    text = re.sub(r"Ê\^([A-Za-z\u0391-\u03c9])", lambda match: "$" + r"\hat{E}^{" + match.group(1) + "}$", text)
    parts = re.split(r"(\$[^$\n]+\$|\\\([^\n]*?\\\)|\\\[[\s\S]*?\\\])", text)
    pattern = r"(?<![A-Za-z0-9_$\\\u0391-\u03c9])([A-Za-z\u0391-\u03c9\u211d\u2220]+(?:[_^](?:\{[^{}\n]+\}|[A-Za-z0-9\u0391-\u03c9\u22a4]))+)"
    for index in range(0, len(parts), 2):
        parts[index] = re.sub(pattern, lambda match: "$" + match.group(1).replace("×", r"\times ").replace("⊤", r"\top ") + "$", parts[index])
    return "".join(parts)


def _numeric_tokens(text: str) -> list[str]:
    text = text.replace(r"\%", "%")
    text = re.sub(r"\\[A-Za-z]+\*?", " ", text)
    months = {name: index for index, name in enumerate(("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"), 1)}
    months.update({name[:3]: index for name, index in list(months.items())})
    text = re.sub(r"\b(?:" + "|".join(months) + r")\b\.?", lambda match: str(months[match.group().lower().rstrip(".")]), text, flags=re.I)
    text = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", text)
    text = re.sub(r"(?<=\d)\s*,\s*(?=\d{3}(?:\D|$))", "", text)
    text = re.sub(r"(?<=\d)\s+(?=%)", "", text)
    text = re.sub(r"(?<![A-Za-z0-9_.])\.\s*(?=\d)", "0.", text)
    text = re.sub(r"(\d+(?:\.\d+)?)\s*([万亿])", lambda match: format(float(match.group(1)) * {"万": 10000, "亿": 100000000}[match.group(2)], ".12g"), text)
    text = re.sub(r"(\d+(?:\.\d+)?)\s*(K|M|B|thousand|million|billion)\b", lambda match: format(float(match.group(1)) * {"k": 1000, "m": 1000000, "b": 1000000000, "thousand": 1000, "million": 1000000, "billion": 1000000000}[match.group(2).lower()], ".12g"), text, flags=re.I)
    return re.findall(r"(?<![A-Za-z\d_.])(?:\d{2,}(?:\.\d+)?%?|\d+\.\d+%?)(?![A-Za-z\d]|\.\d)", text)


def _balanced_latex(latex: str) -> bool:
    return bool(latex.strip()) and latex.count("{") == latex.count("}") and "$$" not in latex


def _figures_redundant(left: FigureIR, right: FigureIR) -> bool:
    left_caption = _normalize_text(left.caption)
    right_caption = _normalize_text(right.caption)
    caption_similarity = SequenceMatcher(None, left_caption, right_caption).ratio()
    same_type = left.analysis.visual_type == right.analysis.visual_type != "unknown"
    left_terms = set(re.findall(r"[a-zA-Z]{4,}|[\u4e00-\u9fff]{2,}", left.analysis.summary.lower()))
    right_terms = set(re.findall(r"[a-zA-Z]{4,}|[\u4e00-\u9fff]{2,}", right.analysis.summary.lower()))
    overlap = len(left_terms & right_terms) / max(1, len(left_terms | right_terms))
    return caption_similarity >= 0.82 or (same_type and overlap >= 0.72)


def _is_explicit_future_claim(claim: EvidenceClaimIR) -> bool:
    if claim.claim_type != "future_work" or not claim.author_asserted:
        return False
    text = claim.claim.lower()
    return bool(re.search(r"未来|后续|下一步|展望|future|further work|open problem|limitation", text))
