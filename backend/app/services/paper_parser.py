from __future__ import annotations

import gzip
import io
import logging
import re
import tarfile
import time
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import fitz
import requests

from app.services.arxiv_service import CandidatePaper
from app.services.paper_models import (
    EquationIR,
    FigureIR,
    PaperIR,
    PaperMetadataIR,
    SectionIR,
    TextBlockIR,
)

logger = logging.getLogger(__name__)

PAPER_REQUEST_TIMEOUT = (8, 35)
SOURCE_REQUEST_TIMEOUT = (8, 25)


class PaperParser:
    CAPTION_RE = re.compile(r"^\s*(?P<label>(?:figure|fig\.?|图)\s*[A-Za-z]?\d+[A-Za-z]?)\s*[:.\-]?\s*", re.I)
    NUMBERED_HEADING_RE = re.compile(r"^\s*(?:\d+(?:\.\d+){0,3}|[IVX]+)\.?\s+\S+", re.I)
    SECTION_WORDS = {
        "abstract",
        "introduction",
        "background",
        "method",
        "methodology",
        "data",
        "experiment",
        "experiments",
        "results",
        "discussion",
        "conclusion",
        "conclusions",
        "appendix",
        "摘要",
        "引言",
        "方法",
        "数据",
        "实验",
        "结果",
        "讨论",
        "结论",
    }

    def download_pdf(self, pdf_url: str, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        response = self._get_with_retry(pdf_url, timeout=PAPER_REQUEST_TIMEOUT)
        content = response.content
        if len(content) < 1024 or not content.lstrip().startswith(b"%PDF"):
            raise RuntimeError("Downloaded arXiv document is not a valid PDF.")
        output_path.write_bytes(content)
        return output_path

    def download_source(self, arxiv_id: str, output_dir: Path) -> Path | None:
        output_dir.mkdir(parents=True, exist_ok=True)
        if any(output_dir.rglob("*.tex")):
            return output_dir
        base_id = arxiv_id.split("v", 1)[0]
        url = f"https://export.arxiv.org/e-print/{base_id}"
        try:
            response = self._get_with_retry(url, timeout=SOURCE_REQUEST_TIMEOUT)
            payload = response.content
            if not payload or len(payload) > 120 * 1024 * 1024:
                return None
            self._extract_source_payload(payload, output_dir)
            return output_dir if any(output_dir.rglob("*.tex")) else None
        except Exception as exc:
            logger.warning("arXiv source download failed for %s: %s", arxiv_id, exc)
            return None

    @staticmethod
    def _get_with_retry(url: str, *, timeout: tuple[int, int]) -> requests.Response:
        urls = [url]
        parsed = urlparse(url)
        if parsed.hostname in {"arxiv.org", "export.arxiv.org"}:
            alternate_host = "arxiv.org" if parsed.hostname == "export.arxiv.org" else "export.arxiv.org"
            urls.append(urlunparse(parsed._replace(netloc=alternate_host)))

        errors: list[str] = []
        for attempt in range(2):
            target = urls[attempt % len(urls)]
            try:
                response = requests.get(
                    target,
                    timeout=timeout,
                    headers={"User-Agent": "AutoSci/1.0"},
                )
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                errors.append(f"{target}: {exc}")
                logger.warning("Paper download failed; retrying once if available: %s", errors[-1])
                if attempt == 0:
                    time.sleep(1)
        raise RuntimeError("Paper download failed after two bounded attempts: " + " | ".join(errors))

    def parse(self, paper: CandidatePaper, pdf_path: Path, output_dir: Path, source_dir: Path | None = None) -> PaperIR:
        output_dir.mkdir(parents=True, exist_ok=True)
        doc = fitz.open(pdf_path)
        try:
            raw_blocks, body_size = self._read_text_blocks(doc)
            sections, blocks = self._build_sections(paper, raw_blocks, body_size)
            figures = self._extract_figures(doc, raw_blocks, blocks, output_dir / "figures")
        finally:
            doc.close()

        equations: list[EquationIR] = []
        math_macros: dict[str, tuple[int, str]] = {}
        structured_source: list[str] = []
        warnings: list[str] = []
        if source_dir:
            math_macros = self._read_math_macros("\n".join(text for _, text in self._read_tex_files(source_dir)))
            equations = self._extract_tex_equations(source_dir)
            self._enrich_captions_from_tex(figures, source_dir)
            structured_source = self._extract_structured_source(source_dir)
        else:
            warnings.append("arXiv source was unavailable; equations are not included unless reliably parsed from TeX.")

        return PaperIR(
            metadata=PaperMetadataIR(
                arxiv_id=paper.arxiv_id,
                title=paper.title,
                authors=paper.authors,
                abstract=paper.abstract,
                categories=paper.categories,
                url=paper.url,
                pdf_url=paper.pdf_url,
            ),
            sections=sections,
            blocks=blocks,
            figures=figures,
            equations=equations,
            math_macros=math_macros,
            structured_source=structured_source,
            parse_warnings=warnings,
        )

    @classmethod
    def _extract_structured_source(cls, source_dir: Path) -> list[str]:
        packets = []
        pattern = re.compile(r"\\begin\{(?P<env>table\*?|assume|assumption|thm|theorem|lemma|prop|proposition|cor|corollary|proof)\}[\s\S]*?\\end\{(?P=env)\}")
        for path, source in cls._read_tex_files(source_dir):
            headings = []
            for heading in re.finditer(r"\\section\*?\s*(?:\[[^\]]*\]\s*)?", source):
                group = cls._tex_group(source, heading.end())
                if group:
                    headings.append((heading.start(), group[0]))
            section = ""
            previous = 0
            for match in pattern.finditer(source):
                titles = [title for offset, title in headings if previous <= offset < match.start()]
                if titles:
                    section = titles[-1]
                previous = match.start()
                text = match.group()
                if len(text) <= 14000:
                    # The surrounding prose distinguishes omitted proofs, prior
                    # results, and separate approximation sequences on one page.
                    preceding = source[max(0, match.start() - 240):match.start()].strip()
                    packets.append(f"Original TeX ({path.name}; section: {section}). Preserve row/column and equation associations:\nPreceding source context:\n{preceding}\n{text}")
        return packets

    @staticmethod
    def _read_text_blocks(doc: fitz.Document) -> tuple[list[dict], float]:
        raw: list[dict] = []
        weighted_sizes: list[float] = []
        for page_index, page in enumerate(doc):
            page_dict = page.get_text("dict", sort=True)
            for block in page_dict.get("blocks", []):
                if block.get("type") != 0:
                    continue
                lines = block.get("lines", [])
                spans = [span for line in lines for span in line.get("spans", []) if span.get("text")]
                text = " ".join(str(span.get("text", "")).strip() for span in spans)
                text = re.sub(r"\s+", " ", text).strip()
                if not text:
                    continue
                sizes = [float(span.get("size", 0.0) or 0.0) for span in spans]
                chars = [max(1, len(str(span.get("text", "")))) for span in spans]
                weighted_sizes.extend(size for size, count in zip(sizes, chars) for _ in range(min(count, 80)))
                raw.append(
                    {
                        "page": page_index + 1,
                        "text": text,
                        "bbox": [round(float(value), 2) for value in block.get("bbox", [])],
                        "max_size": max(sizes, default=0.0),
                    }
                )
        weighted_sizes.sort()
        body_size = weighted_sizes[len(weighted_sizes) // 2] if weighted_sizes else 10.0
        return raw, body_size

    def _build_sections(
        self,
        paper: CandidatePaper,
        raw_blocks: list[dict],
        body_size: float,
    ) -> tuple[list[SectionIR], list[TextBlockIR]]:
        sections = [SectionIR(id="sec-abstract", title="Abstract", page_start=1, page_end=1)]
        blocks = [
            TextBlockIR(
                id="abstract-001",
                section_id="sec-abstract",
                page=1,
                text=paper.abstract,
                kind="abstract",
            )
        ]
        sections[0].block_ids.append("abstract-001")

        current = SectionIR(id="sec-001", title="Paper body", page_start=1, page_end=1)
        sections.append(current)
        paragraph_index = 0
        section_index = 1
        for item in raw_blocks:
            text = item["text"]
            is_caption = bool(self.CAPTION_RE.match(text))
            is_heading = not is_caption and self._is_heading(text, item["max_size"], body_size)
            if is_heading:
                section_index += 1
                current = SectionIR(
                    id=f"sec-{section_index:03d}",
                    title=text[:180],
                    level=self._heading_level(text),
                    page_start=item["page"],
                    page_end=item["page"],
                )
                sections.append(current)
                kind = "heading"
            else:
                kind = "caption" if is_caption else "paragraph"

            paragraph_index += 1
            block_id = f"p-{item['page']:03d}-{paragraph_index:04d}"
            block = TextBlockIR(
                id=block_id,
                section_id=current.id,
                page=item["page"],
                text=text,
                bbox=item["bbox"],
                kind=kind,
            )
            blocks.append(block)
            current.block_ids.append(block_id)
            current.page_end = item["page"]
        return sections, blocks

    def _is_heading(self, text: str, max_size: float, body_size: float) -> bool:
        normalized = text.strip().rstrip(":")
        if not normalized or len(normalized) > 180 or normalized.endswith((".", ";")):
            return False
        lowered = normalized.lower()
        if lowered in self.SECTION_WORDS:
            return True
        if self.NUMBERED_HEADING_RE.match(normalized) and len(normalized.split()) <= 16:
            return True
        return max_size >= body_size * 1.22 and len(normalized.split()) <= 18

    @staticmethod
    def _heading_level(text: str) -> int:
        match = re.match(r"^\s*(\d+(?:\.\d+)*)", text)
        return min(3, match.group(1).count(".") + 1) if match else 1

    def _extract_figures(
        self,
        doc: fitz.Document,
        raw_blocks: list[dict],
        text_blocks: list[TextBlockIR],
        output_dir: Path,
    ) -> list[FigureIR]:
        output_dir.mkdir(parents=True, exist_ok=True)
        block_lookup = {(block.page, block.text): block for block in text_blocks}
        figures: list[FigureIR] = []
        seen_labels: set[str] = set()
        for item in raw_blocks:
            match = self.CAPTION_RE.match(item["text"])
            if not match:
                continue
            if re.match(r"(?:summari[sz]es|shows|illustrates|presents|reports|compares)\b", item["text"][match.end():], re.I):
                continue
            label = re.sub(r"\s+", " ", match.group("label")).strip()
            normalized_label = label.lower().replace(".", "")
            if normalized_label in seen_labels:
                continue
            seen_labels.add(normalized_label)
            page = doc[item["page"] - 1]
            caption_rect = fitz.Rect(item["bbox"])
            clip, source_kind = self._figure_clip(page, caption_rect)
            if clip.width < page.rect.width * 0.18 or clip.height < page.rect.height * 0.08:
                continue
            try:
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2.2, 2.2), clip=clip, alpha=False)
                image_bytes = pixmap.tobytes("png")
            except Exception:
                continue
            figure_id = f"fig-{len(figures) + 1:03d}"
            path = output_dir / f"{figure_id}.png"
            path.write_bytes(image_bytes)

            context_ids = self._nearby_context_ids(text_blocks, item["page"], caption_rect, item["text"])
            caption_block = block_lookup.get((item["page"], item["text"]))
            if caption_block and caption_block.id not in context_ids:
                context_ids.append(caption_block.id)
            reference_count = self._count_figure_references(text_blocks, label, item["text"])
            figures.append(
                FigureIR(
                    id=figure_id,
                    source_label=label,
                    page=item["page"],
                    caption=item["text"],
                    image_path=str(path.resolve()),
                    width=pixmap.width,
                    height=pixmap.height,
                    source_kind=source_kind,
                    context_block_ids=context_ids,
                    reference_count=reference_count,
                )
            )
        return figures

    @staticmethod
    def _figure_clip(page: fitz.Page, caption: fitz.Rect) -> tuple[fitz.Rect, str]:
        page_rect = page.rect
        text_blocks = [block for block in page.get_text("blocks") if block[6] == 0]
        text_rects = [fitz.Rect(block[:4]) for block in text_blocks]
        caption = fitz.Rect(caption)
        # Inline maths can split one caption into overlapping PDF text blocks.
        # Follow those fragments so the screenshot cannot end mid-caption.
        for rect in sorted(text_rects, key=lambda item: item.y0):
            if caption.y0 <= rect.y0 <= caption.y1 + 1 and rect.x1 >= caption.x0 and rect.x0 <= caption.x1:
                caption |= rect
        image_rects: list[fitz.Rect] = []
        for image in page.get_images(full=True):
            try:
                image_rects.extend(page.get_image_rects(image[0]))
            except Exception:
                continue
        drawing_rects = [fitz.Rect(item["rect"]) for item in page.get_drawings() if item.get("rect")]
        preceding_captions = [fitz.Rect(block[:4]) for block in text_blocks
            if PaperParser.CAPTION_RE.match(block[4]) and block[3] < caption.y0
            and block[2] > caption.x0 and block[0] < caption.x1]
        upper_bound = max([page_rect.y0, caption.y0 - page_rect.height * 0.58,
                           *(rect.y1 for rect in preceding_captions)])
        content = [
            rect
            for rect in [*image_rects, *drawing_rects]
            if rect.width > 3
            and rect.height > 3
            and rect.y1 <= caption.y1 + 4
            and rect.y0 >= upper_bound - 4
            and rect.width < page_rect.width * 0.99
            and rect.height < page_rect.height * 0.95
        ]
        if content:
            figure_rect = content[0]
            for rect in content[1:]:
                figure_rect |= rect
            source_kind = "caption_content_crop"
        else:
            figure_rect = fitz.Rect(
                page_rect.x0 + page_rect.width * 0.07,
                max(page_rect.y0, caption.y0 - page_rect.height * 0.42),
                page_rect.x1 - page_rect.width * 0.07,
                caption.y0,
            )
            source_kind = "caption_region_fallback"
        clip = figure_rect | caption
        clip.x0 = max(page_rect.x0, clip.x0 - 12)
        clip.x1 = min(page_rect.x1, clip.x1 + 12)
        clip.y0 = max(page_rect.y0, clip.y0 - 12)
        if preceding_captions:
            clip.y0 = max(clip.y0, max(rect.y1 for rect in preceding_captions) + 0.5)
        clip.y1 = min(page_rect.y1, clip.y1 + 8)
        following = [rect.y0 for rect in [*text_rects, *image_rects, *drawing_rects]
            if rect.y0 > caption.y1 + 0.5 and rect.x1 > clip.x0 and rect.x0 < clip.x1]
        if following:
            clip.y1 = min(clip.y1, min(following) - 0.5)
        if any(rect.y0 >= caption.y0 and rect.y0 <= caption.y1 + 0.5 and rect.y1 > caption.y1
               for rect in [*image_rects, *drawing_rects]):
            clip.y1 = min(clip.y1, caption.y1)
        for rect in text_rects:
            if clip.y0 < rect.y1 <= figure_rect.y0 and rect.width > page_rect.width * 0.3:
                clip.y0 = min(figure_rect.y0, rect.y1 + 2)
        return clip, source_kind

    @staticmethod
    def _nearby_context_ids(
        blocks: list[TextBlockIR],
        page_number: int,
        caption_rect: fitz.Rect,
        caption_text: str,
    ) -> list[str]:
        candidates: list[tuple[float, str]] = []
        for block in blocks:
            if block.kind not in {"paragraph", "caption"} or abs(block.page - page_number) > 1:
                continue
            if block.text == caption_text:
                continue
            if block.page == page_number and len(block.bbox) == 4:
                rect = fitz.Rect(block.bbox)
                distance = min(abs(rect.y1 - caption_rect.y0), abs(rect.y0 - caption_rect.y1))
            else:
                distance = 1000.0 + abs(block.page - page_number) * 100.0
            candidates.append((distance, block.id))
        candidates.sort(key=lambda item: item[0])
        return [block_id for _, block_id in candidates[:4]]

    @staticmethod
    def _count_figure_references(blocks: list[TextBlockIR], label: str, caption_text: str) -> int:
        number_match = re.search(r"\d+", label)
        if not number_match:
            return 0
        number = re.escape(number_match.group(0))
        pattern = re.compile(rf"\b(?:figure|fig\.?)\s*{number}\b|图\s*{number}", re.I)
        return sum(1 for block in blocks if block.text != caption_text and pattern.search(block.text))

    @staticmethod
    def _extract_source_payload(payload: bytes, output_dir: Path) -> None:
        try:
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
                total = 0
                count = 0
                root = output_dir.resolve()
                allowed = {".tex", ".bib", ".sty", ".cls", ".png", ".jpg", ".jpeg", ".pdf", ".eps"}
                for member in archive.getmembers():
                    if not member.isfile() or Path(member.name).suffix.lower() not in allowed:
                        continue
                    count += 1
                    total += max(0, member.size)
                    if count > 2500 or total > 200 * 1024 * 1024:
                        raise RuntimeError("arXiv source archive exceeds extraction safety limits.")
                    target = (root / member.name).resolve()
                    if root not in target.parents:
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source:
                        target.write_bytes(source.read())
                return
        except tarfile.TarError:
            pass

        try:
            content = gzip.decompress(payload)
        except OSError:
            content = payload
        if b"\\document" in content or b"\\begin{document}" in content:
            (output_dir / "main.tex").write_bytes(content)

    @staticmethod
    def _read_tex_files(source_dir: Path) -> list[tuple[Path, str]]:
        outputs: list[tuple[Path, str]] = []
        for path in sorted(source_dir.rglob("*.tex")):
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = path.read_text(encoding="latin-1", errors="ignore")
            text = re.sub(r"(?<!\\)%.*", "", text)
            outputs.append((path, text))
        return outputs

    def _extract_tex_equations(self, source_dir: Path) -> list[EquationIR]:
        equations: list[EquationIR] = []
        source_files = list(self._read_tex_files(source_dir))
        macros = self._read_math_macros("\n".join(text for _, text in source_files))
        supported_environments = {
            "equation",
            "equation*",
            "align",
            "align*",
            "alignat",
            "alignat*",
            "gather",
            "gather*",
            "multline",
            "multline*",
            "split",
            "aligned",
            "alignedat",
            "cases",
            "dcases",
            "array",
        }
        environment_pattern = re.compile(
            r"\\begin\{(?P<env>equation\*?|align\*?|alignat\*?|gather\*?|multline\*?|split|aligned(?:at)?|cases|dcases|array)\}"
            r"(?P<body>.*?)\\end\{(?P=env)\}",
            re.S,
        )
        patterns = (
            environment_pattern,
            re.compile(r"\\\[(?P<body>.*?)\\\]", re.S),
            re.compile(r"(?<!\\)\$\$(?P<body>.*?)\$\$", re.S),
            re.compile(r"(?<!\\)\\\((?P<body>.*?)(?<!\\)\\\)", re.S),
        )
        for path, text in source_files:
            for pattern in patterns:
                for match in pattern.finditer(text):
                    environment = match.groupdict().get("env") if match.groupdict() else None
                    if environment and environment not in supported_environments:
                        continue
                    latex = match.group("body").strip()
                    if environment and environment not in {"equation", "equation*"}:
                        rendered_env = {"align": "aligned", "alignat": "alignedat", "gather": "gathered", "multline": "gathered", "dcases": "cases"}.get(environment.rstrip("*"), environment)
                        latex = f"\\begin{{{rendered_env}}}{latex}\\end{{{rendered_env}}}"
                    latex = re.sub(r"\\(?:label|tag)\{[^{}]*\}", "", latex).strip()
                    latex = self._expand_math_macros(latex, macros)
                    if not self._valid_latex_fragment(latex):
                        continue
                    normalized = re.sub(r"\s+", " ", latex)
                    if any(re.sub(r"\s+", " ", item.latex) == normalized for item in equations):
                        continue
                    start = max(0, match.start() - 240)
                    end = min(len(text), match.end() + 240)
                    context = re.sub(r"\s+", " ", text[start:end]).strip()
                    equations.append(
                        EquationIR(
                            id=f"eq-{len(equations) + 1:03d}",
                            latex=latex[:3000],
                            source_file=str(path.relative_to(source_dir)),
                            context=context[:600],
                        )
                    )
                    if len(equations) >= 80:
                        return equations
        return equations

    @staticmethod
    def _tex_group(text: str, start: int) -> tuple[str, int] | None:
        while start < len(text) and text[start].isspace():
            start += 1
        if start >= len(text) or text[start] != "{":
            return None
        depth = 1
        for index in range(start + 1, len(text)):
            if text[index] in "{}" and (index == 0 or text[index - 1] != "\\"):
                depth += 1 if text[index] == "{" else -1
                if depth == 0:
                    return text[start + 1:index], index + 1
        return None

    @classmethod
    def _read_math_macros(cls, text: str) -> dict[str, tuple[int, str]]:
        macros: dict[str, tuple[int, str]] = {}
        pattern = re.compile(r"\\(?:newcommand|renewcommand|providecommand)\*?\s*\{(?P<name>\\[A-Za-z]+)\}\s*(?:\[(?P<args>[0-9])\])?\s*")
        for match in pattern.finditer(text):
            group = cls._tex_group(text, match.end())
            if group:
                macros[match.group("name")] = (int(match.group("args") or 0), group[0])
        for match in re.finditer(r"\\DeclareMathOperator(?P<star>\*)?\s*\{(?P<name>\\[A-Za-z]+)\}\s*", text):
            group = cls._tex_group(text, match.end())
            if group:
                macros[match.group("name")] = (0, "\\operatorname" + ("*" if match.group("star") else "") + "{" + group[0] + "}")
        for match in re.finditer(r"\\def\s*(?P<name>\\[A-Za-z]+)\s*", text):
            group = cls._tex_group(text, match.end())
            if group:
                macros[match.group("name")] = (0, group[0])
        return macros

    @classmethod
    def _expand_math_macros(cls, latex: str, macros: dict[str, tuple[int, str]]) -> str:
        for _ in range(8):
            parts, cursor = [], 0
            for match in re.finditer(r"\\[A-Za-z]+", latex):
                if match.start() < cursor or match.group() not in macros:
                    continue
                count, replacement = macros[match.group()]
                end = match.end()
                arguments = []
                for _argument in range(count):
                    group = cls._tex_group(latex, end)
                    if group is None:
                        break
                    arguments.append(group[0])
                    end = group[1]
                if len(arguments) != count:
                    continue
                for index, argument in enumerate(arguments, 1):
                    replacement = replacement.replace(f"#{index}", argument)
                parts.extend((latex[cursor:match.start()], replacement))
                cursor = end
            parts.append(latex[cursor:])
            expanded = "".join(parts)
            if expanded == latex or len(expanded) > 12000:
                break
            latex = expanded
        return re.sub(r"\\mathds\b", r"\\mathbb", latex)

    @staticmethod
    def _valid_latex_fragment(latex: str) -> bool:
        if not latex or len(latex) > 3000 or latex.count("{") != latex.count("}"):
            return False
        return bool(
            re.search(
                r"[=<>]|\\(?:sum|prod|int|frac|dfrac|tfrac|mathbb|mathbf|mathrm|operatorname|begin|cases|left|right)",
                latex,
            )
        )

    def _enrich_captions_from_tex(self, figures: list[FigureIR], source_dir: Path) -> None:
        captions: list[str] = []
        pattern = re.compile(r"\\caption(?:\[[^\]]*\])?\{((?:[^{}]|\{[^{}]*\})+)\}", re.S)
        environments = re.compile(r"\\begin\{figure\*?\}(.*?)\\end\{figure\*?\}", re.S)
        for _, text in self._read_tex_files(source_dir):
            for environment in environments.finditer(text):
                captions.extend(re.sub(r"\s+", " ", match.group(1)).strip() for match in pattern.finditer(environment.group(1)))

        def normalized(caption: str) -> str:
            caption = self.CAPTION_RE.sub("", caption).replace(r"\&", "&")
            caption = re.sub(r"\\[A-Za-z]+\*?", "", caption)
            return " ".join(re.findall(r"\w+", caption.lower()))

        # TeX file order and float order can differ; table captions must never
        # shift figure associations. Retain the PDF caption if matching is weak.
        for figure in figures:
            original = normalized(figure.caption)
            matches = sorted(
                ((SequenceMatcher(None, original, normalized(caption)).ratio(), caption) for caption in captions),
                reverse=True,
            )
            if not matches or matches[0][0] < 0.72:
                continue
            if len(matches) > 1 and matches[0][0] < 1.0 and matches[0][0] - matches[1][0] < 0.08:
                continue
            figure.caption = matches[0][1][:2000]
