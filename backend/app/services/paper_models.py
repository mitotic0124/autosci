from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


ClaimType = Literal[
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
]


class PaperMetadataIR(BaseModel):
    arxiv_id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    abstract: str = ""
    categories: list[str] = Field(default_factory=list)
    url: str = ""
    pdf_url: str = ""


class TextBlockIR(BaseModel):
    id: str
    section_id: str
    page: int
    text: str
    bbox: list[float] = Field(default_factory=list)
    kind: Literal["abstract", "heading", "paragraph", "caption"] = "paragraph"


class SectionIR(BaseModel):
    id: str
    title: str
    level: int = 1
    block_ids: list[str] = Field(default_factory=list)
    page_start: int = 1
    page_end: int = 1


class FigureAnalysisIR(BaseModel):
    visual_type: str = "unknown"
    summary: str = ""
    key_findings: list[str] = Field(default_factory=list)
    legibility: float = 0.5
    importance: float = 0.5
    decorative: bool = False
    confidence: float = 0.5
    vision_verified: bool = False
    caption_verified: bool = False


class FigureIR(BaseModel):
    id: str
    source_label: str
    page: int
    caption: str
    image_path: str
    artifact_url: str = ""
    width: int = 0
    height: int = 0
    source_kind: str = "caption_crop"
    context_block_ids: list[str] = Field(default_factory=list)
    reference_count: int = 0
    analysis: FigureAnalysisIR = Field(default_factory=FigureAnalysisIR)
    related_claim_ids: list[str] = Field(default_factory=list)
    score: float = 0.0
    selected: bool = False


class EquationIR(BaseModel):
    id: str
    latex: str
    source_file: str = ""
    context: str = ""


class PaperIR(BaseModel):
    schema_version: str = "1.0"
    metadata: PaperMetadataIR
    sections: list[SectionIR] = Field(default_factory=list)
    blocks: list[TextBlockIR] = Field(default_factory=list)
    figures: list[FigureIR] = Field(default_factory=list)
    equations: list[EquationIR] = Field(default_factory=list)
    math_macros: dict[str, tuple[int, str]] = Field(default_factory=dict)
    structured_source: list[str] = Field(default_factory=list)
    parse_warnings: list[str] = Field(default_factory=list)


class ClaimEvidenceIR(BaseModel):
    block_id: str
    page: int
    quote: str


class EvidenceClaimIR(BaseModel):
    id: str
    claim: str
    claim_type: ClaimType
    evidence: list[ClaimEvidenceIR] = Field(default_factory=list)
    author_asserted: bool = True
    confidence: float = 0.5
    verified: bool = False


class EvidenceLedger(BaseModel):
    schema_version: str = "1.0"
    claims: list[EvidenceClaimIR] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class FigurePlacementIR(BaseModel):
    figure_id: str
    after_claim_id: str
    reason: str = ""


class ArticleSectionPlanIR(BaseModel):
    id: str
    title: str
    purpose: str
    claim_ids: list[str] = Field(default_factory=list)
    figure_placements: list[FigurePlacementIR] = Field(default_factory=list)
    equation_ids: list[str] = Field(default_factory=list)
    target_words: int = 450


class ArticlePlan(BaseModel):
    schema_version: str = "1.0"
    title: str
    sections: list[ArticleSectionPlanIR] = Field(default_factory=list)


class ArticleBlockIR(BaseModel):
    id: str
    type: Literal["paragraph", "figure", "equation"] = "paragraph"
    text: str = ""
    claim_ids: list[str] = Field(default_factory=list)
    asset_id: str | None = None


class ArticleSectionIR(BaseModel):
    id: str
    title: str
    blocks: list[ArticleBlockIR] = Field(default_factory=list)


class ArticleDocument(BaseModel):
    schema_version: str = "1.0"
    title: str
    authors: list[str] = Field(default_factory=list)
    arxiv_url: str = ""
    sections: list[ArticleSectionIR] = Field(default_factory=list)


class QualityIssueIR(BaseModel):
    code: str
    message: str
    severity: Literal["warning", "error"] = "error"
    block_id: str | None = None


class QualityReport(BaseModel):
    schema_version: str = "1.0"
    passed: bool = False
    score: float = 0.0
    mode: Literal["verified", "extractive_fallback", "failed"] = "failed"
    metrics: dict[str, float | int | bool] = Field(default_factory=dict)
    issues: list[QualityIssueIR] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


@dataclass
class ArticlePackage:
    document: ArticleDocument
    markdown: str
    selected_image_urls: list[str]
    quality_report: QualityReport
    artifact_paths: dict[str, Path]
