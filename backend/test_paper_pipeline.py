import json
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path

import fitz
import requests

from app.core.config import Settings
from app.services.arxiv_service import CandidatePaper
from app.services.llm_service import LLMProviderUnavailable, LLMService
from app.services.paper_models import (
    ArticleBlockIR,
    ArticleDocument,
    ArticlePlan,
    ArticleSectionPlanIR,
    ArticleSectionIR,
    ClaimEvidenceIR,
    EvidenceClaimIR,
    EvidenceLedger,
    EquationIR,
    FigureAnalysisIR,
    FigureIR,
    PaperIR,
    PaperMetadataIR,
)
from app.services.paper_parser import PaperParser
from app.services.paper_pipeline import PaperUnderstandingPipeline, _normalize_inline_math, _normalize_text, _numeric_tokens


def _candidate() -> CandidatePaper:
    return CandidatePaper(
        arxiv_id="2607.00001v1",
        title="A Verified Forecasting Method",
        authors=["Ada Researcher", "Bo Analyst"],
        abstract=(
            "We introduce a forecasting model for asset returns. "
            "The model combines a robust estimator with rolling validation. "
            "We evaluate it on a public market dataset. "
            "The empirical comparison shows lower forecast error than the benchmark."
        ),
        categories=["q-fin.ST"],
        published_at=datetime.now(timezone.utc),
        url="https://arxiv.org/abs/2607.00001v1",
        pdf_url="https://arxiv.org/pdf/2607.00001v1.pdf",
    )


def _write_synthetic_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 64), "A Verified Forecasting Method", fontsize=18)
    page.insert_text((72, 108), "1 Introduction", fontsize=14)
    page.insert_text(
        (72, 136),
        "The paper proposes a robust forecasting model and evaluates it with rolling validation.",
        fontsize=10,
    )
    page.insert_text((72, 166), "2 Empirical Results", fontsize=14)
    page.insert_text(
        (72, 194),
        "As shown in Figure 1, the proposed model has lower forecast error than the benchmark.",
        fontsize=10,
    )
    page.draw_rect(fitz.Rect(90, 235, 520, 455), color=(0, 0, 0), width=1)
    page.draw_line((120, 410), (240, 330), color=(0.1, 0.4, 0.7), width=3)
    page.draw_line((240, 330), (360, 350), color=(0.1, 0.4, 0.7), width=3)
    page.draw_line((360, 350), (490, 280), color=(0.1, 0.4, 0.7), width=3)
    page.insert_text(
        (92, 480),
        "Figure 1. Out-of-sample forecast errors for the proposed model and benchmark.",
        fontsize=10,
    )
    page.insert_text((72, 535), "3 Conclusion", fontsize=14)
    page.insert_text(
        (72, 563),
        "The results support the reported forecasting improvement on the evaluated dataset.",
        fontsize=10,
    )
    doc.save(path)
    doc.close()


class FakeStructuredLLM:
    enabled = True

    def generate_vision_json(self, system_prompt, user_prompt, image_path, *, model=None):
        return {
            "visual_type": "line_chart",
            "summary": "该图比较模型与基准的样本外预测误差。",
            "key_findings": ["模型曲线对应较低的预测误差"],
            "legibility": 0.95,
            "importance": 0.95,
            "decorative": False,
            "confidence": 0.95,
        }

    def generate_json(self, system_prompt, user_prompt, *, model=None, reasoning_budget=None):
        if "证据抽取器" in system_prompt:
            return {
                "claims": [
                    {
                        "claim": "论文提出了一种资产收益预测模型。",
                        "claim_type": "contribution",
                        "evidence_block_ids": ["abstract-001"],
                        "author_asserted": True,
                        "confidence": 0.95,
                    },
                    {
                        "claim": "该模型结合稳健估计与滚动验证。",
                        "claim_type": "method",
                        "evidence_block_ids": ["abstract-001"],
                        "author_asserted": True,
                        "confidence": 0.95,
                    },
                    {
                        "claim": "研究使用公开市场数据集进行评估。",
                        "claim_type": "data",
                        "evidence_block_ids": ["abstract-001"],
                        "author_asserted": True,
                        "confidence": 0.95,
                    },
                    {
                        "claim": "实证比较显示该模型的预测误差低于基准。",
                        "claim_type": "empirical_result",
                        "evidence_block_ids": ["abstract-001"],
                        "author_asserted": True,
                        "confidence": 0.95,
                    },
                ]
            }
        if "独立事实核验员" in system_prompt:
            return {
                "verdicts": [
                    {"id": f"claim-{index:03d}", "supported": True, "confidence": 0.95}
                    for index in range(1, 5)
                ]
            }
        if "图文关系标注员" in system_prompt:
            return {"links": [{"figure_id": "fig-001", "claim_ids": ["claim-004"]}]}
        if "论文解读编辑" in system_prompt:
            return {
                "title": "稳健资产收益预测模型及其实证表现",
                "sections": [
                    {
                        "title": "研究设计与主要结果",
                        "purpose": "解释方法、数据和核心实证结果。",
                        "claim_ids": ["claim-001", "claim-002", "claim-003", "claim-004"],
                        "figure_placements": [
                            {
                                "figure_id": "fig-001",
                                "after_claim_id": "claim-004",
                                "reason": "图中直接比较预测误差。",
                            }
                        ],
                        "equation_ids": [],
                        "target_words": 450,
                    }
                ],
            }
        if "证据约束的中文科技编辑" in system_prompt:
            return {
                "blocks": [
                    {"text": "论文提出资产收益预测模型，并将稳健估计与滚动验证结合。", "claim_ids": ["claim-001", "claim-002"]},
                    {"text": "研究使用公开市场数据集进行评估。", "claim_ids": ["claim-003"]},
                    {"text": "实证比较显示，该模型的预测误差低于基准。", "claim_ids": ["claim-004"]},
                ]
            }
        if "独立文章事实核验员" in system_prompt:
            block_ids = []
            marker = "内容："
            raw = user_prompt.split(marker, 1)[-1]
            try:
                block_ids = [item["block_id"] for item in json.loads(raw)]
            except Exception:
                pass
            return {"verdicts": [{"block_id": block_id, "supported": True, "issues": []} for block_id in block_ids]}
        return None


class PaperParserTests(unittest.TestCase):
    def test_download_pdf_retries_on_arxiv_alternate_host(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.pdf"
            failed = requests.Timeout("temporary timeout")
            response = Mock(content=b"%PDF" + b" " * 1200)
            response.raise_for_status.return_value = None
            with patch("app.services.paper_parser.requests.get", side_effect=[failed, response]) as get, \
                 patch("app.services.paper_parser.time.sleep"):
                PaperParser().download_pdf("https://arxiv.org/pdf/2607.00001v1.pdf", path)

            self.assertEqual(b"%PDF" + b" " * 1200, path.read_bytes())
            self.assertEqual("https://export.arxiv.org/pdf/2607.00001v1.pdf", get.call_args_list[1].args[0])
            self.assertEqual((8, 35), get.call_args_list[0].kwargs["timeout"])

    def test_structured_tex_keeps_optional_nested_section_titles_and_proofs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "main.tex").write_text(
                r"\section{Preliminaries}"
                r"\section[Finite G-BSDEs]{Finite-horizon \textit{G}-BSDEs}"
                r"\begin{thm}The finite solution is unique.\end{thm}"
                r"\begin{proof}Define the limit as $n\to\infty$.\end{proof}"
                r"\section[Infinite G-BSDEs]{Infinite-horizon \textit{G}-BSDEs}"
                r"This proof is omitted by Ito's formula."
                r"\begin{prop}The constructed coefficients are bounded.\end{prop}", encoding="utf-8")
            packets = PaperParser._extract_structured_source(root)
            self.assertEqual(3, len(packets))
            self.assertIn(r"section: Finite-horizon \textit{G}-BSDEs", packets[1])
            self.assertIn(r"\begin{proof}Define the limit as $n\to\infty$.", packets[1])
            self.assertIn(r"section: Infinite-horizon \textit{G}-BSDEs", packets[2])
            self.assertIn("This proof is omitted by Ito's formula.", packets[2])

    def test_structured_tex_preserves_table_rows_and_theorem_conditions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "main.tex").write_text(
                r"\section{Results}\begin{table}\begin{tabular}{lr}A & 1.20 \\ C & 1.27\end{tabular}\end{table}"
                r"\begin{thm}Suppose $k\ge 2$. Then $x=y$.\end{thm}", encoding="utf-8")
            packets = PaperParser._extract_structured_source(root)
            self.assertEqual(2, len(packets))
            self.assertIn(r"A & 1.20 \\ C & 1.27", packets[0])
            self.assertIn(r"$k\ge 2$", packets[1])

    def test_crop_stays_between_neighboring_figures(self) -> None:
        page = Mock()
        page.rect = fitz.Rect(0, 0, 612, 792)
        page.get_images.return_value = []
        page.get_drawings.return_value = [{"rect": fitz.Rect(70, top, 540, bottom)} for top, bottom in [(80, 200), (240, 420), (454, 630)]]
        page.get_text.return_value = [
            (70, 210, 540, 228, "Figure 1: previous result", 0, 0),
            (70, 434, 540, 450, "Figure 2: current result", 1, 0),
            (70, 640, 540, 658, "Figure 3: next result", 2, 0),
        ]
        clip, _ = PaperParser._figure_clip(page, fitz.Rect(70, 434, 540, 450))
        self.assertGreater(clip.y0, 228)
        self.assertLessEqual(clip.y0, 240)
        self.assertGreater(clip.y1, 450)
        self.assertLess(clip.y1, 454)
        page.get_drawings.return_value[-1]["rect"] = fitz.Rect(70, 449, 540, 630)
        clip, _ = PaperParser._figure_clip(page, fitz.Rect(70, 434, 540, 450))
        self.assertEqual(450, clip.y1)
        next_clip, _ = PaperParser._figure_clip(page, fitz.Rect(70, 640, 540, 658))
        self.assertGreater(next_clip.y0, 450)
        self.assertLess(next_clip.width, 510)

    def test_reader_caption_uses_verified_translation_instead_of_visual_inference(self) -> None:
        llm = Mock()
        llm.generate_json.return_value = {"captions": [{"figure_id": "fig-001", "text": "基于小时收益率的风险价值可视化。"}]}
        pipeline = PaperUnderstandingPipeline(llm, Settings(llm_provider="disabled"))
        figure = FigureIR(id="fig-001", source_label="Figure 1", page=1,
            caption="Value at Risk visualizations utilizing hourly returns.", image_path="figure.png",
            analysis=FigureAnalysisIR(vision_verified=True, summary="风险随时间变化。"))
        pipeline._verify_claims = Mock(return_value={"fig-001": (True, 1.0)})
        pipeline._verify_figure_captions([figure])
        self.assertTrue(figure.analysis.caption_verified)
        self.assertEqual("基于小时收益率的风险价值可视化。", figure.analysis.summary)
        pipeline._verify_claims.return_value = {"fig-001": (False, 1.0)}
        pipeline._verify_figure_captions([figure])
        self.assertFalse(figure.analysis.caption_verified)

    def test_crop_includes_fragmented_caption_without_previous_paragraph(self) -> None:
        page = Mock()
        page.rect = fitz.Rect(0, 0, 612, 792)
        page.get_images.return_value = []
        page.get_drawings.return_value = [{"rect": fitz.Rect(86, 313, 526, 533)}]
        page.get_text.return_value = [
            (86, 268, 526, 302, "Previous paragraph", 0, 0),
            (86, 559, 526, 581, "Figure 1: first caption fragment", 1, 0),
            (86, 576, 526, 642, "Caption continued around inline mathematics", 2, 0),
            (86, 631, 526, 653, "Last caption line", 3, 0),
            (86, 665, 526, 689, "Next paragraph", 4, 0),
        ]
        clip, _ = PaperParser._figure_clip(page, fitz.Rect(86, 559, 526, 581))
        self.assertGreater(clip.y0, 302)
        self.assertLessEqual(clip.y0, 313)
        self.assertGreater(clip.y1, 653)
        self.assertLess(clip.y1, 665)

    def test_tex_tables_and_float_order_cannot_relabel_figures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "main.tex").write_text(
                r"\begin{figure}\caption{Bullish BTC prices with trading signals.}\end{figure}"
                r"\begin{table}\caption{Returns by market regime.}\end{table}"
                r"\begin{figure}\caption{Bearish BTC prices with trading signals.}\end{figure}",
                encoding="utf-8",
            )
            figures = [
                FigureIR(id="fig-002", source_label="Fig. 2", page=2,
                         caption="Fig. 2. Bearish BTC prices with trading signals.", image_path="second.png"),
                FigureIR(id="fig-001", source_label="Fig. 1", page=1,
                         caption="Fig. 1. Bullish BTC prices with trading signals.", image_path="first.png"),
                FigureIR(id="fig-003", source_label="Fig. 3", page=3,
                         caption="Fig. 3. Unrelated calibration experiment.", image_path="third.png"),
            ]
            PaperParser()._enrich_captions_from_tex(figures, root)
            self.assertEqual("Bearish BTC prices with trading signals.", figures[0].caption)
            self.assertEqual("Bullish BTC prices with trading signals.", figures[1].caption)
            self.assertEqual("Fig. 3. Unrelated calibration experiment.", figures[2].caption)

    def test_source_macros_expand_without_losing_math_semantics(self) -> None:
        definitions = r"\newcommand{\R}{\mathbb{R}}\newcommand{\E}{\mathbb{E}}\newcommand{\norm}[1]{\left\|#1\right\|}\DeclareMathOperator{\col}{col}"
        macros = PaperParser._read_math_macros(definitions)
        expanded = PaperParser._expand_math_macros(r"x\in\R, \E[X]=\norm{x}, \col(B)", macros)
        self.assertEqual(r"x\in\mathbb{R}, \mathbb{E}[X]=\left\|x\right\|, \operatorname{col}(B)", expanded)

    def test_caption_crop_preserves_source_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf_path = root / "paper.pdf"
            _write_synthetic_pdf(pdf_path)

            paper_ir = PaperParser().parse(_candidate(), pdf_path, root / "parsed")

            self.assertEqual(1, len(paper_ir.figures))
            figure = paper_ir.figures[0]
            self.assertEqual("Figure 1", figure.source_label)
            self.assertGreaterEqual(figure.reference_count, 1)
            self.assertTrue(Path(figure.image_path).exists())
            self.assertGreater(figure.width, 600)
            self.assertIn("forecast errors", figure.caption)
            self.assertTrue(figure.context_block_ids)

    def test_extracts_extended_tex_equation_environments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir)
            (source / "main.tex").write_text(
                r"""\begin{document}
                \begin{cases} x+y &= 1 \\ x-y &= 0 \end{cases}
                $$ z = \frac{1}{2}x^2 $$
                \end{document}""",
                encoding="utf-8",
            )
            equations = PaperParser()._extract_tex_equations(source)
            self.assertEqual(2, len(equations))
            self.assertTrue(any("cases" not in item.latex for item in equations))


class PaperPipelineTests(unittest.TestCase):
    def test_production_provider_failure_retains_known_unresolved_findings(self) -> None:
        from app.services.paper_models import QualityIssueIR, QualityReport
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "paper.pdf"
            pdf.touch()
            paper = PaperIR(metadata=PaperMetadataIR(arxiv_id="x", title="x"))
            document = ArticleDocument(title="中文解读", sections=[ArticleSectionIR(id="s", title="方法", blocks=[ArticleBlockIR(id="b", text="需要重新核验的错误条件。", claim_ids=["c"])])])
            ledger = EvidenceLedger(claims=[EvidenceClaimIR(id="c", claim="原文条件。", claim_type="assumption")])
            pipeline = PaperUnderstandingPipeline(Mock(enabled=True), Settings(llm_provider="disabled", article_fetch_arxiv_source=False))
            pipeline._parser.parse = Mock(return_value=paper)
            pipeline._analyze_figures = Mock()
            pipeline._build_evidence_ledger = Mock(return_value=(ledger, True))
            pipeline._link_figures_to_claims = Mock()
            pipeline._select_figures = Mock()
            pipeline._build_article_plan = Mock(return_value=ArticlePlan(title="中文"))
            pipeline._draft_document = Mock(return_value=(document, True))
            def fail_after_checkpoint(*args, **kwargs):
                pipeline._write_json(root / "qa_report.json", QualityReport(passed=False, mode="failed", score=0, issues=[QualityIssueIR(code="article_context_mismatch", block_id="b", message="Known unsupported assumption")]))
                raise LLMProviderUnavailable("HTTP 402")
            pipeline._verify_and_repair = Mock(side_effect=fail_after_checkpoint)
            with self.assertRaises(LLMProviderUnavailable):
                pipeline.run(_candidate(), pdf, root)
            report = QualityReport.model_validate_json((root / "qa_report.json").read_text(encoding="utf-8"))
            self.assertFalse(report.passed)
            self.assertEqual({"article_context_mismatch", "llm_provider_unavailable"}, {issue.code for issue in report.issues})

    def test_provider_failure_preserves_repaired_checkpoint_without_passing_it(self) -> None:
        settings = Settings(llm_provider="disabled", article_min_chars=1, article_min_paragraphs=1, article_min_sections=1)
        pipeline = PaperUnderstandingPipeline(Mock(enabled=True), settings)
        document = ArticleDocument(title="中文解读", sections=[ArticleSectionIR(id="s", title="模型", blocks=[ArticleBlockIR(id="b", text="原文条件被错误地描述为目标价格有界。", claim_ids=["c"])])])
        ledger = EvidenceLedger(claims=[EvidenceClaimIR(id="c", claim="原文仅要求利率有界。", claim_type="assumption")])
        pipeline._verify_article_blocks = Mock(side_effect=[{"b": (True, [])}, LLMProviderUnavailable("HTTP 402")])
        pipeline._review_article_context = Mock(return_value={"b": (False, ["Incorrect boundedness assumption"])})
        pipeline._review_global_consistency = Mock(return_value=[])
        repaired = "原文对构造的利率系数施加有界性条件。"
        pipeline._repair_block = Mock(return_value=repaired)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(LLMProviderUnavailable):
                pipeline._verify_strict_document(document, PaperIR(metadata=PaperMetadataIR(arxiv_id="x", title="x")), ledger, ArticlePlan(title="中文"), evidence_verified=True, drafting_complete=True, checkpoint_dir=Path(directory))
            saved = json.loads((Path(directory) / "rejected_document.json").read_text(encoding="utf-8"))
            report = json.loads((Path(directory) / "qa_report.json").read_text(encoding="utf-8"))
            self.assertEqual(repaired, saved["sections"][0]["blocks"][0]["text"])
            self.assertFalse(report["passed"])
            self.assertFalse(report["metrics"]["article_global_verified"])
            self.assertEqual({"section": "模型", "preceding": "", "following": ""},
                             pipeline._repair_block.call_args.kwargs["article_context"])

    def test_source_section_reference_is_distinct_from_missing_article_section(self) -> None:
        paper = PaperIR(metadata=PaperMetadataIR(arxiv_id="x", title="x"))
        ledger = EvidenceLedger(claims=[EvidenceClaimIR(id="c", claim="规模依赖性在后续章节讨论。", claim_type="method")])
        for text, rejected in [("规模依赖性将在下一节讨论。", True), ("规模依赖性在原文的下一节讨论。", False), ("原论文中下一节讨论规模依赖性。", False)]:
            document = ArticleDocument(title="中文", sections=[ArticleSectionIR(id="s", title="局限", blocks=[ArticleBlockIR(id="b", text=text, claim_ids=["c"])])])
            issues = PaperUnderstandingPipeline._deterministic_issues(document, paper, ledger)
            self.assertEqual(rejected, "dangling_section_reference" in {issue.code for issue in issues})

    def test_known_error_cannot_disappear_without_a_changed_repaired_paragraph(self) -> None:
        settings = Settings(llm_provider="disabled", article_min_chars=1, article_min_paragraphs=1, article_min_sections=1, article_max_repair_attempts=2)
        pipeline = PaperUnderstandingPipeline(Mock(enabled=True), settings)
        document = ArticleDocument(title="中文解读", sections=[ArticleSectionIR(id="s", title="模型", blocks=[ArticleBlockIR(id="b", text="原文条件被错误地描述为目标价格有界。", claim_ids=["c"])])])
        ledger = EvidenceLedger(claims=[EvidenceClaimIR(id="c", claim="原文仅要求利率有界。", claim_type="assumption")])
        pipeline._verify_article_blocks = Mock(return_value={"b": (True, [])})
        pipeline._review_article_context = Mock(side_effect=[
            {"b": (False, ["Incorrect boundedness assumption"])},
            {"b": (True, [])},
            {"b": (True, [])},
        ])
        pipeline._review_global_consistency = Mock(return_value=[])
        pipeline._repair_block = Mock(return_value=None)
        with self.assertRaisesRegex(RuntimeError, "Incorrect boundedness assumption"):
            pipeline._verify_strict_document(document, PaperIR(metadata=PaperMetadataIR(arxiv_id="x", title="x")), ledger, ArticlePlan(title="中文"), evidence_verified=True, drafting_complete=True)
        self.assertEqual(settings.article_max_repair_attempts + 1, pipeline._review_article_context.call_count)

    def test_final_figure_alignment_uses_actual_paragraph_content(self) -> None:
        llm = Mock(enabled=True)
        llm.generate_json.return_value = {"placements": [{"figure_id": "fig1", "after_block_id": "result", "reason": "Matches the plotted return comparison"}]}
        pipeline = PaperUnderstandingPipeline(llm, Settings(llm_provider="disabled"))
        doc = ArticleDocument(title="中文", sections=[
            ArticleSectionIR(id="s1", title="结果", blocks=[ArticleBlockIR(id="result", text="收益对比")]),
            ArticleSectionIR(id="s2", title="未来工作", blocks=[ArticleBlockIR(id="future", text="后续研究"), ArticleBlockIR(id="figure", type="figure", asset_id="fig1")]),
        ])
        paper = PaperIR(metadata=PaperMetadataIR(arxiv_id="x", title="x"), figures=[FigureIR(id="fig1", source_label="Figure 1", page=1, caption="Return comparison", image_path="x.png", selected=True)])
        pipeline._align_article_figures(doc, paper)
        self.assertEqual(["result", "figure"], [b.id for b in doc.sections[0].blocks])
        self.assertEqual(["future"], [b.id for b in doc.sections[1].blocks])
        llm.generate_json.return_value = {"placements": []}
        with self.assertRaisesRegex(RuntimeError, "placement review"):
            pipeline._align_article_figures(doc, paper)

    def test_short_article_is_expanded_by_model_and_fully_reverified(self) -> None:
        settings = Settings(llm_provider="disabled", article_min_chars=100, article_min_paragraphs=1, article_min_sections=1)
        pipeline = PaperUnderstandingPipeline(Mock(enabled=True), settings)
        document = ArticleDocument(title="中文解读", sections=[ArticleSectionIR(id="s1", title="方法", blocks=[ArticleBlockIR(id="b1", text="原文给出模型假设。", claim_ids=["c1"])])])
        ledger = EvidenceLedger(claims=[EvidenceClaimIR(id="c1", claim="模型假设与变量定义。", claim_type="method", verified=True)])
        pipeline._verify_article_blocks = Mock(return_value={"b1": (True, [])})
        pipeline._review_article_context = Mock(return_value={"b1": (True, [])})
        pipeline._review_global_consistency = Mock(return_value=[])
        pipeline._repair_block = Mock(return_value="该研究讨论模型的状态变量及其可识别条件。" * 8)
        repaired, report = pipeline._verify_strict_document(document, PaperIR(metadata=PaperMetadataIR(arxiv_id="x", title="x")), ledger, ArticlePlan(title="中文解读"), evidence_verified=True, drafting_complete=True)
        self.assertTrue(report.passed)
        self.assertGreaterEqual(len(repaired.sections[0].blocks[0].text), 100)
        self.assertEqual(2, pipeline._verify_article_blocks.call_count)
        self.assertEqual(2, pipeline._review_global_consistency.call_count)

    def test_equation_is_inserted_only_after_its_explanatory_paragraph(self) -> None:
        llm = Mock(enabled=True)
        llm.generate_json.return_value = {"blocks": [
            {"text": "问题背景", "claim_ids": ["c1"]},
            {"text": "方程说明", "claim_ids": ["c1"], "equation_ids_after": ["eq1", "unknown"]},
        ]}
        pipeline = PaperUnderstandingPipeline(llm, Settings(llm_provider="disabled"))
        paper = PaperIR(metadata=PaperMetadataIR(arxiv_id="x", title="x"), equations=[EquationIR(id="eq1", latex="x=y"), EquationIR(id="eq2", latex="a=b")])
        ledger = EvidenceLedger(claims=[EvidenceClaimIR(id="c1", claim="证据", claim_type="method")])
        plan = ArticlePlan(title="中文", sections=[ArticleSectionPlanIR(id="s1", title="模型", purpose="解释", claim_ids=["c1"], equation_ids=["eq1", "eq2"])])
        document, complete = pipeline._draft_document(paper, ledger, plan)
        self.assertTrue(complete)
        self.assertEqual(["paragraph", "paragraph", "equation"], [b.type for b in document.sections[0].blocks])
        self.assertEqual("eq1", document.sections[0].blocks[-1].asset_id)

    def test_incomplete_context_verdict_is_retried_and_still_fails_closed(self) -> None:
        llm = Mock(enabled=True)
        complete = {"verdicts": [{"block_id": "b1", "supported": True, "issues": []}]}
        llm.generate_json.side_effect = [{"verdicts": []}, complete]
        pipeline = PaperUnderstandingPipeline(llm, Settings(llm_provider="disabled"))
        document = ArticleDocument(title="Review", sections=[ArticleSectionIR(id="s1", title="Method", blocks=[ArticleBlockIR(id="b1", text="A claim", claim_ids=["c1"])])])
        paper = PaperIR(metadata=PaperMetadataIR(arxiv_id="x", title="x"))
        ledger = EvidenceLedger()
        self.assertEqual({"b1": (True, [])}, pipeline._review_article_context(document, paper, None, ledger))
        self.assertEqual(2, llm.generate_json.call_count)
        llm.generate_json.side_effect = None
        llm.generate_json.return_value = {"verdicts": []}
        self.assertIsNone(pipeline._review_article_context(document, paper, None, ledger))

    def test_invalid_batch_splits_without_omitting_any_verdict(self) -> None:
        llm = Mock(enabled=True)
        llm.generate_json.side_effect = [{}, {},
            {"verdicts": [{"block_id": "b1", "supported": True, "issues": []}]},
            {"verdicts": [{"block_id": "b2", "supported": False, "issues": ["wrong condition"]}]},
        ]
        pipeline = PaperUnderstandingPipeline(llm, Settings(llm_provider="disabled"))
        document = ArticleDocument(title="Review", sections=[ArticleSectionIR(id="s1", title="Method", blocks=[
            ArticleBlockIR(id="b1", text="First claim", claim_ids=["c1"]),
            ArticleBlockIR(id="b2", text="Second claim", claim_ids=["c1"]),
        ])])
        result = pipeline._review_article_context(document, PaperIR(metadata=PaperMetadataIR(arxiv_id="x", title="x")), None, EvidenceLedger())
        self.assertEqual({"b1": (True, []), "b2": (False, ["wrong condition"])}, result)
        self.assertEqual(4, llm.generate_json.call_count)

    def test_conflicting_verdict_flags_cannot_pass_review(self) -> None:
        llm = Mock(enabled=True)
        pipeline = PaperUnderstandingPipeline(llm, Settings(llm_provider="disabled"))
        block = ArticleBlockIR(id="b1", text="A claim", claim_ids=["c1"])
        document = ArticleDocument(title="Review", sections=[ArticleSectionIR(id="s1", title="Method", blocks=[block])])
        paper = PaperIR(metadata=PaperMetadataIR(arxiv_id="x", title="x"))
        for supported, issues in [(True, ["Wrong attribution"]), (False, []), (False, [""])]:
            with self.subTest(supported=supported, issues=issues):
                llm.generate_json.return_value = {"verdicts": [{"block_id": "b1", "supported": supported, "issues": issues}]}
                self.assertIsNone(pipeline._review_article_context(document, paper, None, EvidenceLedger()))
                self.assertIsNone(pipeline._verify_article_blocks([block], {}, None))

    def test_global_audit_rejects_attribution_errors_and_incomplete_schema(self) -> None:
        llm = Mock(enabled=True)
        llm.generate_json.return_value = {"checks": {"attribution": False, "scope": True, "numerics": True, "conclusions": False, "proof_attribution": True, "discourse": True}, "issues": [{"block_id": "b1", "message": "Prior single-factor result is attributed to this paper."}]}
        pipeline = PaperUnderstandingPipeline(llm, Settings(llm_provider="disabled"))
        document = ArticleDocument(title="Review", sections=[ArticleSectionIR(id="s1", title="Conclusion", blocks=[ArticleBlockIR(id="b1", text="A claim")])])
        issues = pipeline._review_global_consistency(document, EvidenceLedger(), None)
        self.assertEqual("article_global_mismatch", issues[0].code)
        self.assertEqual("b1", issues[0].block_id)
        llm.generate_json.return_value = {"checks": {"attribution": True}, "issues": []}
        self.assertIsNone(pipeline._review_global_consistency(document, EvidenceLedger(), None))

    def test_non_boolean_verification_cannot_mark_a_claim_supported(self) -> None:
        llm = Mock(enabled=True)
        llm.generate_json.return_value = {"verdicts": [{"id": "claim-001", "supported": "false", "confidence": 1.0}]}
        pipeline = PaperUnderstandingPipeline(llm, Settings(llm_provider="disabled"))
        claim = EvidenceClaimIR(id="claim-001", claim="A misstated numerical result", claim_type="numeric_result")
        self.assertFalse(pipeline._verify_claims([claim])[claim.id][0])

    def test_global_contradiction_blocks_publication_despite_local_verdicts(self) -> None:
        settings = Settings(llm_provider="disabled", article_min_paragraphs=1, article_min_sections=1, article_min_chars=1)
        pipeline = PaperUnderstandingPipeline(Mock(enabled=True), settings)
        block = ArticleBlockIR(id="b1", text="样本量和维度同时增加。", claim_ids=["c1"])
        document = ArticleDocument(title="中文", sections=[ArticleSectionIR(id="s1", title="模型", blocks=[block])])
        ledger = EvidenceLedger(claims=[EvidenceClaimIR(id="c1", claim="样本量固定，维度增加。", claim_type="assumption", verified=True)])
        paper = PaperIR(metadata=PaperMetadataIR(arxiv_id="x", title="x"))
        pipeline._verify_article_blocks = Mock(return_value={"b1": (True, [])})
        pipeline._review_article_context = Mock(return_value={"b1": (False, ["原文固定样本量，不能同时增大。"])})
        pipeline._repair_block = Mock(return_value=block.text)
        with self.assertRaisesRegex(RuntimeError, "quality gate failed"):
            pipeline._verify_and_repair(document, paper, ledger, ArticlePlan(title="中文"), evidence_verified=True, drafting_complete=True)
        self.assertEqual(settings.article_max_repair_attempts + 1, pipeline._review_article_context.call_count)

    def test_generated_inline_math_expands_source_macros(self) -> None:
        paper = PaperIR(metadata=PaperMetadataIR(arxiv_id="x", title="x"), math_macros={r"\Pib": (0, r"\Pi")})
        document = ArticleDocument(title="中文", sections=[ArticleSectionIR(id="s", title="方法", blocks=[ArticleBlockIR(id="b", text=r"投影为 $\Pib h_j$。", claim_ids=["c"])])])
        markdown = PaperUnderstandingPipeline._render_markdown(document, paper)
        self.assertIn(r"$\Pi h_j$", markdown)
        self.assertNotIn(r"\Pib", markdown)

    def test_planner_assigns_claims_and_formulas_to_one_section(self) -> None:
        settings = Settings(llm_provider="disabled")
        pipeline = PaperUnderstandingPipeline(LLMService(settings), settings)
        paper = PaperIR(metadata=PaperMetadataIR(arxiv_id="x", title="x"), equations=[EquationIR(id="eq1", latex="x=y")])
        ledger = EvidenceLedger(claims=[EvidenceClaimIR(id=f"c{i}", claim=f"已验证证据{i}", claim_type="method") for i in range(3)])
        payload = {"title": "中文解读", "sections": [
            {"title": "模型", "claim_ids": ["c0", "c1"], "equation_ids": ["eq1"]},
            {"title": "估计", "claim_ids": ["c1", "c2"], "equation_ids": ["eq1"]},
        ]}
        plan = pipeline._validated_plan(payload, paper, ledger)
        self.assertEqual(["c2"], plan.sections[1].claim_ids)
        self.assertEqual([], plan.sections[1].equation_ids)
        self.assertEqual(2, len(plan.sections))

    def test_quality_gate_detects_repeated_equations(self) -> None:
        paper = PaperIR(metadata=PaperMetadataIR(arxiv_id="x", title="x"))
        document = ArticleDocument(title="中文", sections=[ArticleSectionIR(id="s1", title="模型", blocks=[
            ArticleBlockIR(id="b1", type="equation", text="x = y"),
            ArticleBlockIR(id="b2", type="equation", text="x=y"),
        ])])
        issues = PaperUnderstandingPipeline._deterministic_issues(document, paper, EvidenceLedger())
        self.assertIn("duplicate_equation", {issue.code for issue in issues})

    def test_strict_gate_rejects_short_english_article(self) -> None:
        claim = EvidenceClaimIR(id="c1", claim="A model is evaluated.", claim_type="method", evidence=[])
        document = ArticleDocument(title="English title", sections=[ArticleSectionIR(id="s1", title="Method", blocks=[ArticleBlockIR(id="b1", text="This model is evaluated using a sequence of observations from financial markets.", claim_ids=["c1"])])])
        issues = PaperUnderstandingPipeline._deterministic_issues(document, PaperIR(metadata=PaperMetadataIR(arxiv_id="x", title="x")), EvidenceLedger(claims=[claim]), strict_quality=True)
        self.assertTrue({"non_chinese_paragraph", "article_too_short", "article_section_coverage", "article_char_count"}.issubset({issue.code for issue in issues}))

    def test_internal_equation_ids_cannot_leak_into_reader_text(self) -> None:
        claim = EvidenceClaimIR(id="claim-002", claim="公式 002 描述因子模型。", claim_type="method")
        document = ArticleDocument(title="中文解读", sections=[ArticleSectionIR(id="s1", title="模型", blocks=[
            ArticleBlockIR(id="b1", text="该模型见 eq-002。", claim_ids=[claim.id]),
        ])])
        issues = PaperUnderstandingPipeline._deterministic_issues(document, PaperIR(metadata=PaperMetadataIR(arxiv_id="x", title="x")), EvidenceLedger(claims=[claim]), strict_quality=True)
        self.assertIn("internal_reference_residue", {issue.code for issue in issues})

    def test_math_source_in_prose_requires_renderable_delimiters(self) -> None:
        claim = EvidenceClaimIR(id="c", claim="模型使用协方差和载荷。", claim_type="method")
        paper = PaperIR(metadata=PaperMetadataIR(arxiv_id="x", title="x"))
        for text, rejected in [
            ("总体协方差为 Σ_f，载荷位于 R^{p×k}。", True),
            (r"变量满足 \Sigma_f 正定。", True),
            (r"总体协方差为 $\Sigma_f$，载荷位于 $\mathbb{R}^{p\times k}$。", False),
            ("总体因子协方差正定，载荷矩阵具有列满秩。", False),
        ]:
            with self.subTest(text=text):
                document = ArticleDocument(title="中文解读", sections=[ArticleSectionIR(id="s", title="模型", blocks=[ArticleBlockIR(id="b", text=text, claim_ids=["c"])])])
                issues = PaperUnderstandingPipeline._deterministic_issues(document, paper, EvidenceLedger(claims=[claim]), strict_quality=True)
                self.assertEqual(rejected, "unrendered_inline_math" in {issue.code for issue in issues})

    def test_placeholder_repair_cannot_replace_article_text(self) -> None:
        llm = Mock()
        llm.generate_json.return_value = {"text": "修复后的段落"}
        pipeline = PaperUnderstandingPipeline(llm, Settings(llm_provider="disabled"))
        original = "原文说明此估计量仅在固定样本量条件下适用，不能推广到其他渐近框架。"
        block = ArticleBlockIR(id="b1", text=original, claim_ids=["c1"])
        claim = EvidenceClaimIR(id="c1", claim=original, claim_type="assumption", verified=True)
        self.assertIsNone(pipeline._repair_block(block, {"c1": claim}, ["Clarify the condition"]))
        self.assertEqual(original, block.text)
        llm.generate_json.return_value = {"paragraph": original}
        self.assertEqual(original, pipeline._repair_block(block, {"c1": claim}, ["Clarify the condition"]))
        llm.generate_json.return_value = {"text": {"unsupported": original}}
        self.assertIsNone(pipeline._repair_block(block, {"c1": claim}, ["Clarify the condition"]))

    def test_corrupted_json_math_is_blocked_even_if_it_would_render(self) -> None:
        paper = PaperIR(metadata=PaperMetadataIR(arxiv_id="x", title="x"))
        ledger = EvidenceLedger(claims=[EvidenceClaimIR(id="c", claim="原文定义滑点。", claim_type="method")])
        for formula, rejected in [(r"$ ext{Slippage}=frac12 x$", True),
                                  ("$\x0crac{1}{2}x$", True),
                                  (r"$\text{Slippage}=\frac12 x$", False)]:
            document = ArticleDocument(title="中文解读", sections=[ArticleSectionIR(id="s", title="方法", blocks=[ArticleBlockIR(id="b", text="滑点定义为" + formula, claim_ids=["c"])])])
            issues = PaperUnderstandingPipeline._deterministic_issues(document, paper, ledger, strict_quality=True)
            self.assertEqual(rejected, "corrupted_math" in {issue.code for issue in issues})

    def test_numeric_evidence_handles_sentence_periods_and_chinese_adjacency(self) -> None:
        self.assertEqual(["3000", "8.5", "93%"], _numeric_tokens("fixed p = 3,000. Errors: 8 . 5 degrees, 93 %."))
        self.assertEqual(["1000", "3000", "8.5"], _numeric_tokens("在1000条路径中，维度为3000，误差降至8.5度。"))
        self.assertEqual([], _numeric_tokens("x12 factor_20"))
        self.assertEqual(["12", "2025"], _numeric_tokens("December 2025"))
        self.assertEqual(["100000", "15000", "200000000"], _numeric_tokens("初始资本10万美元，1.5万份，2亿元。"))
        self.assertEqual(["100000", "1500000", "2000000000"], _numeric_tokens("$100K, 1.5 million, 2B"))
        self.assertEqual(["0.07", "0.96%"], _numeric_tokens("Fee = .07 C P(1-P), return -.96%"))
        self.assertEqual(["0.07"], _numeric_tokens(r"\lceil.07 C P(1-P)\rceil"))
        self.assertEqual(["0.07"], _numeric_tokens("Fee = ⌈ . 07 C P (1 − P ) ⌉"))
        self.assertEqual(["12", "2025", "25.2%"], _numeric_tokens(r"Dec. 2025 & 25.2\%"))

    def test_inline_math_normalization_preserves_existing_math_and_semantics(self) -> None:
        text = r"总体协方差 Σ_f = B^⊤，方向 b_j 位于 R^{p×k}，已有 $x_i$。"
        normalized = _normalize_inline_math(text)
        self.assertEqual(r"总体协方差 $Σ_f$ = $B^\top $，方向 $b_j$ 位于 $R^{p\times k}$，已有 $x_i$。", normalized)
        self.assertEqual(normalized, _normalize_inline_math(normalized))
        self.assertEqual("协方差$Σ_f$正定。", _normalize_inline_math("协方差Σ_f正定。"))
        self.assertEqual("正文（原文第6页）与表1不同。", _normalize_inline_math("正文（p-006-0084）与表1不同。"))
        self.assertEqual(r"$\left\|Y^n - Y\right\|_{L^p}$", _normalize_inline_math(r"‖$Y^n$ - Y‖_{$L^p$}"))
        self.assertEqual(r"$\hat{E}[|K_s|^p]$", _normalize_inline_math(r"ˆE[|$K_s$|^p]"))

    def test_global_audit_retries_self_contradictory_verdict(self) -> None:
        llm = Mock()
        flags = {key: True for key in ("attribution", "scope", "numerics", "conclusions", "proof_attribution", "discourse")}
        llm.generate_json.side_effect = [
            {"checks": {**flags, "numerics": False}, "issues": [{"block_id": "b", "message": "该段无实际错误，应保持原样。"}]},
            {"checks": flags, "issues": []},
            {"passed": True, "issues": []},
        ]
        pipeline = PaperUnderstandingPipeline(llm, Settings(llm_provider="disabled"))
        document = ArticleDocument(title="中文", sections=[ArticleSectionIR(id="s", title="结果", blocks=[ArticleBlockIR(id="b", text="正文与表格分别报告了不同结果。")])])
        self.assertEqual([], pipeline._review_global_consistency(document, EvidenceLedger(), None))
        self.assertEqual(3, llm.generate_json.call_count)

    def test_final_reader_check_blocks_dangling_references_after_factual_pass(self) -> None:
        llm = Mock()
        flags = {key: True for key in ("attribution", "scope", "numerics", "conclusions", "proof_attribution", "discourse")}
        llm.generate_json.side_effect = [
            {"checks": flags, "issues": []},
            {"passed": False, "issues": [{"block_id": "b", "message": "No preceding text introduces this construction."}]},
        ]
        pipeline = PaperUnderstandingPipeline(llm, Settings(llm_provider="disabled"))
        document = ArticleDocument(title="Review", sections=[ArticleSectionIR(id="s", title="Method", blocks=[
            ArticleBlockIR(id="b", text="The construction above proves the result."),
        ])])
        issues = pipeline._review_global_consistency(document, EvidenceLedger(), None)
        self.assertEqual("article_global_mismatch", issues[0].code)
        self.assertEqual("b", issues[0].block_id)
        self.assertIn("No preceding text", issues[0].message)
        llm.generate_json.side_effect = None
        llm.generate_json.return_value = {"passed": True, "issues": [{"block_id": "b", "message": "No antecedent"}]}
        self.assertIsNone(pipeline._review_reader_continuity([{"block_id": "b", "text": "As above"}], None))

    def test_claim_verification_is_complete_across_batches(self) -> None:
        llm = Mock()
        llm.generate_json.side_effect = lambda **kwargs: {"verdicts": [{"id": item["id"], "supported": True, "confidence": 0.9} for item in json.loads(kwargs["user_prompt"].split("待核验内容：", 1)[1])]}
        pipeline = PaperUnderstandingPipeline(llm, Settings(llm_provider="disabled"))
        claims = [EvidenceClaimIR(id=f"claim-{i:03d}", claim="已验证声明", claim_type="method", evidence=[]) for i in range(23)]
        result = pipeline._verify_claims(claims)
        self.assertEqual({claim.id for claim in claims}, set(result))
        self.assertEqual(3, llm.generate_json.call_count)
        llm.generate_json.side_effect = None
        llm.generate_json.return_value = {"verdicts": []}
        self.assertIsNone(pipeline._verify_claims(claims[:1]))

    def test_disabled_llm_uses_safe_extractive_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf_path = root / "paper.pdf"
            _write_synthetic_pdf(pdf_path)
            settings = Settings(
                llm_provider="disabled",
                artifacts_dir=str(root),
                article_fetch_arxiv_source=False,
                article_min_images=0,
            )

            package = PaperUnderstandingPipeline(LLMService(settings), settings).run(
                _candidate(), pdf_path, root / "paper-output"
            )

            self.assertTrue(package.quality_report.passed)
            self.assertEqual("extractive_fallback", package.quality_report.mode)
            self.assertNotIn("下一步", package.markdown)
            self.assertNotIn("未来研究", package.markdown)
            self.assertTrue(package.artifact_paths["paper_ir"].exists())
            self.assertTrue(package.artifact_paths["quality_report"].exists())
            paragraph_blocks = [
                block
                for section in package.document.sections
                for block in section.blocks
                if block.type == "paragraph"
            ]
            self.assertTrue(paragraph_blocks)
            self.assertTrue(all(block.claim_ids for block in paragraph_blocks))

    def test_structured_pipeline_places_verified_figure_after_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf_path = root / "paper.pdf"
            _write_synthetic_pdf(pdf_path)
            settings = Settings(
                llm_provider="disabled",
                llm_vision_model="fake-vision",
                article_require_verified=False,
                artifacts_dir=str(root),
                article_fetch_arxiv_source=False,
                article_min_images=1,
                article_max_images=2,
            )
            pipeline = PaperUnderstandingPipeline(FakeStructuredLLM(), settings)

            package = pipeline.run(_candidate(), pdf_path, root / "paper-output")

            self.assertTrue(package.quality_report.passed)
            self.assertEqual("verified", package.quality_report.mode)
            section = package.document.sections[0]
            figure_index = next(index for index, block in enumerate(section.blocks) if block.type == "figure")
            self.assertGreater(figure_index, 0)
            self.assertIn("claim-004", section.blocks[figure_index - 1].claim_ids)
            self.assertEqual("fig-001", section.blocks[figure_index].asset_id)
            self.assertEqual(1, len(package.selected_image_urls))
            self.assertEqual(1, package.markdown.count("![Figure 1]"))

    def test_quality_gate_rejects_unsupported_numbers_and_future_section(self) -> None:
        claim = EvidenceClaimIR(
            id="claim-001",
            claim="The model improves forecast accuracy.",
            claim_type="conclusion",
            evidence=[ClaimEvidenceIR(block_id="p-001", page=1, quote="The model improves forecast accuracy.")],
            verified=True,
        )
        document = ArticleDocument(
            title="arxiv论文速递|测试",
            sections=[
                ArticleSectionIR(
                    id="s1",
                    title="下一步研究方向",
                    blocks=[
                        ArticleBlockIR(
                            id="b1",
                            text="该模型将准确率提升 37%。",
                            claim_ids=["claim-001"],
                        )
                    ],
                )
            ],
        )
        paper_ir = PaperIR(metadata=PaperMetadataIR(arxiv_id="x", title="x"))

        issues = PaperUnderstandingPipeline._deterministic_issues(
            document,
            paper_ir,
            EvidenceLedger(claims=[claim]),
        )

        self.assertIn("unsupported_number", {issue.code for issue in issues})
        self.assertIn("unsupported_future_section", {issue.code for issue in issues})

    def test_verified_stabilizer_can_restore_length_from_covered_claims(self) -> None:
        settings = Settings(llm_provider="disabled", article_min_chars=1800)
        pipeline = PaperUnderstandingPipeline(LLMService(settings), settings)
        claims = [
            EvidenceClaimIR(
                id=f"claim-{index:03d}",
                claim=("已核验声明 %d：论文明确给出了该方法、数据或结果的具体条件与含义。" % index),
                claim_type="method",
                verified=True,
            )
            for index in range(1, 61)
        ]
        document = ArticleDocument(
            title="arxiv论文速递|测试",
            sections=[ArticleSectionIR(id="s1", title="方法", blocks=[
                ArticleBlockIR(id="b1", text=claims[0].claim, claim_ids=[claims[0].id]),
            ])],
        )
        pipeline._stabilize_verified_document(
            document,
            EvidenceLedger(claims=claims),
            min_paragraphs=4,
            min_chars=1800,
        )
        paragraphs = [block for section in document.sections for block in section.blocks if block.type == "paragraph"]
        self.assertGreaterEqual(len(paragraphs), 4)
        self.assertGreaterEqual(sum(len(block.text) for block in paragraphs), 1800)
        self.assertEqual(len(paragraphs), len({_normalize_text(block.text) for block in paragraphs}))

    def test_plan_cannot_anchor_figure_to_unrelated_claim(self) -> None:
        settings = Settings(llm_provider="disabled")
        pipeline = PaperUnderstandingPipeline(LLMService(settings), settings)
        claim_one = EvidenceClaimIR(
            id="claim-001",
            claim="系统对新闻文章进行聚类。",
            claim_type="method",
            evidence=[ClaimEvidenceIR(block_id="p1", page=5, quote="The system clusters news articles.")],
            verified=True,
        )
        claim_two = EvidenceClaimIR(
            id="claim-002",
            claim="系统每天生成简报。",
            claim_type="method",
            evidence=[ClaimEvidenceIR(block_id="p2", page=2, quote="The system generates a daily brief.")],
            verified=True,
        )
        paper_ir = PaperIR(
            metadata=PaperMetadataIR(arxiv_id="x", title="x"),
            figures=[
                FigureIR(
                    id="fig-001",
                    source_label="Figure 1",
                    page=5,
                    caption="Cluster word map",
                    image_path="figure.png",
                    related_claim_ids=["claim-001"],
                    selected=True,
                )
            ],
        )
        ledger = EvidenceLedger(claims=[claim_one, claim_two])
        payload = {
            "title": "测试",
            "sections": [
                {
                    "title": "方法",
                    "purpose": "测试",
                    "claim_ids": ["claim-001", "claim-002"],
                    "figure_placements": [
                        {"figure_id": "fig-001", "after_claim_id": "claim-002", "reason": "wrong"}
                    ],
                }
            ],
        }

        plan = pipeline._validated_plan(payload, paper_ir, ledger)
        pipeline._ensure_figure_placements(plan, paper_ir, ledger)

        self.assertEqual(1, len(plan.sections[0].figure_placements))
        self.assertEqual("claim-001", plan.sections[0].figure_placements[0].after_claim_id)

    def test_redundant_figures_are_not_both_selected(self) -> None:
        settings = Settings(llm_provider="disabled", article_min_images=1, article_max_images=4)
        pipeline = PaperUnderstandingPipeline(LLMService(settings), settings)
        analysis = FigureAnalysisIR(
            visual_type="word_cloud",
            summary="关于同一组新闻文章的词云图",
            legibility=0.95,
            importance=0.9,
            confidence=0.95,
            vision_verified=True,
        )
        figures = [
            FigureIR(
                id=f"fig-{index:03d}",
                source_label=f"Figure {index}",
                page=index,
                caption="Word map for the same cluster of news articles",
                image_path=f"figure-{index}.png",
                reference_count=2,
                analysis=analysis.model_copy(),
                related_claim_ids=["claim-001"],
            )
            for index in (1, 2)
        ]
        paper_ir = PaperIR(metadata=PaperMetadataIR(arxiv_id="x", title="x"), figures=figures)

        pipeline._select_figures(paper_ir)

        self.assertEqual(1, sum(figure.selected for figure in figures))


class LLMJsonParsingTests(unittest.TestCase):
    def test_account_balance_error_stops_further_requests_in_the_run(self) -> None:
        service = LLMService(Settings(llm_provider="disabled"))
        service._client = Mock()
        error = RuntimeError("insufficient balance")
        error.status_code = 402
        service._client.chat.completions.create.side_effect = error
        for _ in range(2):
            with self.assertRaisesRegex(LLMProviderUnavailable, "余额不足"):
                service.generate_json("Return JSON", "Check")
        self.assertEqual(1, service._client.chat.completions.create.call_count)

    def test_siliconflow_v4_uses_bounded_reasoning_without_affecting_other_models(self) -> None:
        service = LLMService(Settings(llm_provider="disabled", llm_api_base_url="https://api.siliconflow.cn/v1", llm_thinking_budget=2048))
        service._client = Mock()
        create = service._client.chat.completions.create
        create.return_value = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"ok": true}'))])
        for model in ("deepseek-ai/DeepSeek-V4-Flash", "deepseek-ai/DeepSeek-V4-Pro"):
            service.generate_json("Return JSON", "Check", model=model)
            self.assertEqual({"enable_thinking": True, "thinking_budget": 2048}, create.call_args.kwargs["extra_body"])
            service.generate_json("Return JSON", "Audit", model=model, reasoning_budget=8192)
            self.assertEqual(8192, create.call_args.kwargs["extra_body"]["thinking_budget"])
            self.assertGreaterEqual(create.call_args.kwargs["max_tokens"], 12288)
        service.generate_json("Return JSON", "Check", model="another-provider/model")
        self.assertNotIn("extra_body", create.call_args.kwargs)

    def test_unsupported_json_mode_retries_without_format_and_caches_capability(self) -> None:
        service = LLMService(Settings(llm_provider="disabled"))
        service._client = Mock()
        response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"ok": true}'))])
        create = service._client.chat.completions.create
        create.side_effect = [RuntimeError("Json mode is not supported for this model."), response, response]
        for _ in range(2):
            self.assertEqual({"ok": True}, service.generate_json("Return JSON", "Check", model="test-model"))
        self.assertIn("response_format", create.call_args_list[0].kwargs)
        self.assertNotIn("response_format", create.call_args_list[1].kwargs)
        self.assertNotIn("response_format", create.call_args_list[2].kwargs)

    def test_extracts_json_from_fenced_response(self) -> None:
        value = LLMService.parse_json_object('```json\n{"ok": true, "value": 3}\n```')
        self.assertEqual({"ok": True, "value": 3}, value)

    def test_empty_json_retries_and_token_truncation_is_rejected(self) -> None:
        service = LLMService(Settings(llm_provider="disabled"))
        service._client = Mock()
        create = service._client.chat.completions.create
        create.side_effect = [
            SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{}'))]),
            SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"verdicts": []}'))]),
        ]
        self.assertEqual({"verdicts": []}, service.generate_json("Return JSON", "Check"))
        self.assertEqual(create.call_count, 2)
        create.side_effect = None
        create.return_value = SimpleNamespace(choices=[SimpleNamespace(finish_reason="length", message=SimpleNamespace(content='{"ok": true}'))])
        self.assertIsNone(service.generate_json("Return JSON", "Check"))


if __name__ == "__main__":
    unittest.main()
