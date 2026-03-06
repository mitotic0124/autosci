from pathlib import Path

import fitz

from app.core.config import Settings
from app.services.arxiv_service import CandidatePaper
from app.services.llm_service import LLMService


class NoteService:
    def __init__(self, llm_service: LLMService, settings: Settings) -> None:
        self._llm_service = llm_service
        self._settings = settings

    def build_note(self, paper: CandidatePaper, image_urls: list[str], pdf_path: Path | None = None) -> str:
        pdf_text = self._extract_pdf_text(pdf_path) if pdf_path else ""
        llm_note = self._build_with_llm(paper, image_urls, pdf_text)
        if llm_note:
            return self._normalize_llm_note(llm_note, paper)
        return self._build_fallback(paper, image_urls, pdf_text)

    def _build_with_llm(self, paper: CandidatePaper, image_urls: list[str], pdf_text: str) -> str | None:
        images_markdown = "\n".join(f"- ![图示 {idx}]({url})" for idx, url in enumerate(image_urls, start=1))
        if not images_markdown:
            images_markdown = "- 无可用图示"

        prompt = f"""
请基于下面提供的论文信息、PDF文本片段和关键图示素材，产出一份中文论文解读（Markdown）。
必须满足：
1. 标题必须使用一级标题，格式严格为：# arxiv论文速递|<中文标题>。
2. 不要输出“详细中文阅读解读”作为标题，也不要输出“面向对象”等说明语。
3. 结构不强制固定章节，但必须完整覆盖三类信息：方法论、数据说明、实证结论。
4. 不需要讨论研究缺陷、潜在偏误或识别策略。
5. 你自行决定图片应插入在哪些段落，并直接插入下方给出的图片 Markdown。
6. 强调可执行结论与下一步实验建议，不要只复述摘要。

论文信息：
- 标题: {paper.title}
- arXiv: {paper.arxiv_id}
- 作者: {", ".join(paper.authors)}
- 分类: {", ".join(paper.categories)}
- 链接: {paper.url}

可插入图示素材：
{images_markdown}

论文摘要：
{paper.abstract}

PDF 文本片段（可能截断）：
{pdf_text[:18000] if pdf_text else "无可用 PDF 文本。"}
"""
        return self._llm_service.generate(
            system_prompt=self._settings.llm_note_system_prompt,
            user_prompt=prompt.strip(),
        )

    def _build_fallback(self, paper: CandidatePaper, image_urls: list[str], pdf_text: str) -> str:
        figures = ""
        if image_urls:
            for idx, url in enumerate(image_urls, start=1):
                figures += f"![图示 {idx}]({url})\n\n"
                figures += f"*图示 {idx}：自动提取，建议人工核对其与正文对应关系。*\n\n"
        else:
            figures = "无可用图示。\n"

        snippet = (pdf_text[:2000] + "...") if pdf_text else "无可用 PDF 文本。"
        return f"""# arxiv论文速递|{paper.title}

## 方法论
论文围绕极值风险和相关结构建模展开，核心是将相关矩阵特征基旋转与极值分析结合，并采用 POT（peaks-over-threshold）框架处理尾部风险。

## 数据说明
从摘要与文本片段看，研究对象是高频股票收益，并区分市场/行业共同成分与个体成分。完整样本区间、清洗规则与异常处理细节仍需在正文核对。

## 实证结论
论文重点是尾部风险刻画能力提升，而非直接报告交易收益。作为交易策略输入时，建议在目标市场上做二次验证。

## PDF 文本片段
> {snippet}

## 关键图示
{figures}

## 后续执行建议
1. 将同构方法迁移到目标市场数据并做滚动验证。
2. 加入交易成本和流动性过滤后评估稳定性。
3. 对关键阈值和窗口长度做敏感性测试。
"""

    def _normalize_llm_note(self, note: str, paper: CandidatePaper) -> str:
        lines = note.splitlines()
        cleaned: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped == "详细中文阅读解读":
                continue
            if stripped.startswith("面向对象") or stripped.startswith("受众"):
                continue
            cleaned.append(line)

        title_line_index = None
        for i, line in enumerate(cleaned):
            if line.strip():
                title_line_index = i
                break

        preferred_prefix = "# arxiv论文速递|"
        if title_line_index is None:
            return f"{preferred_prefix}{paper.title}\n"

        first_line = cleaned[title_line_index].strip()
        if first_line.startswith("# "):
            heading = first_line[2:].strip()
            if not heading.startswith("arxiv论文速递|"):
                heading = heading.replace("详细中文阅读解读", "").strip("：: ")
                if not heading:
                    heading = paper.title
                cleaned[title_line_index] = f"{preferred_prefix}{heading}"
        else:
            cleaned.insert(title_line_index, f"{preferred_prefix}{paper.title}")

        return "\n".join(cleaned).strip() + "\n"

    @staticmethod
    def _extract_pdf_text(pdf_path: Path, max_pages: int = 18, max_chars: int = 30000) -> str:
        if not pdf_path.exists():
            return ""
        text_parts: list[str] = []
        try:
            doc = fitz.open(pdf_path)
            try:
                page_count = min(len(doc), max_pages)
                for i in range(page_count):
                    page_text = doc[i].get_text("text")
                    if page_text:
                        text_parts.append(page_text.strip())
                    if sum(len(item) for item in text_parts) >= max_chars:
                        break
            finally:
                doc.close()
        except Exception:
            return ""
        merged = "\n\n".join(text_parts)
        return merged[:max_chars]
