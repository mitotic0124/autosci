import hashlib
import re
from pathlib import Path

import fitz
import requests


class PaperImageService:
    def download_pdf(self, pdf_url: str, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        response = requests.get(pdf_url, timeout=60)
        response.raise_for_status()
        output_path.write_bytes(response.content)
        return output_path

    def extract_relevant_images(self, pdf_path: Path, output_dir: Path, max_images: int = 2) -> list[Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        doc = fitz.open(pdf_path)

        candidates: list[dict] = []
        seen_xref: set[int] = set()
        seen_content: set[str] = set()
        try:
            for page_index, page in enumerate(doc):
                for item in page.get_images(full=True):
                    xref = item[0]
                    if xref in seen_xref:
                        continue
                    seen_xref.add(xref)
                    smask = int(item[1] or 0)
                    try:
                        pixmap = fitz.Pixmap(doc, xref)
                        if smask:
                            mask = fitz.Pixmap(doc, smask)
                            pixmap = fitz.Pixmap(pixmap, mask)
                        if pixmap.colorspace and pixmap.colorspace.n > 3:
                            pixmap = fitz.Pixmap(fitz.csRGB, pixmap)
                        image_bytes = pixmap.tobytes("png")
                        width = int(pixmap.width)
                        height = int(pixmap.height)
                    except Exception:
                        continue
                    pixel_area = width * height
                    aspect_ratio = width / max(height, 1)
                    if pixel_area < 60_000 or min(width, height) < 140 or not 0.2 <= aspect_ratio <= 6.0:
                        continue

                    digest = hashlib.sha256(image_bytes).hexdigest()
                    if digest in seen_content:
                        continue
                    seen_content.add(digest)

                    try:
                        display_area = max(
                            (max(0.0, rect.width) * max(0.0, rect.height) for rect in page.get_image_rects(xref)),
                            default=0.0,
                        )
                    except Exception:
                        display_area = 0.0
                    page_area = max(1.0, page.rect.width * page.rect.height)
                    display_ratio = min(1.0, display_area / page_area)
                    score = pixel_area * (1.0 + min(display_ratio * 4.0, 1.5))
                    candidates.append(
                        {
                            "score": score,
                            "page": page_index,
                            "xref": xref,
                            "bytes": image_bytes,
                        }
                    )

            if len(candidates) < max_images:
                candidates.extend(self._extract_vector_figures(doc, seen_content))
        finally:
            doc.close()

        candidates.sort(key=lambda value: float(value["score"]), reverse=True)
        selected = candidates[:max_images]
        selected.sort(key=lambda value: (int(value["page"]), int(value["xref"])))

        outputs: list[Path] = []
        for idx, candidate in enumerate(selected, start=1):
            xref = int(candidate["xref"])
            out = output_dir / f"figure_{idx}_{xref}.png"
            out.write_bytes(candidate["bytes"])
            outputs.append(out)
        return outputs

    @staticmethod
    def _extract_vector_figures(doc: fitz.Document, seen_content: set[str]) -> list[dict]:
        candidates: list[dict] = []
        caption_pattern = re.compile(r"^\s*(?:figure|fig\.?|图)\s*\d+", flags=re.IGNORECASE)
        for page_index, page in enumerate(doc):
            captions = [
                fitz.Rect(block[:4])
                for block in page.get_text("blocks")
                if len(block) >= 5 and caption_pattern.match(str(block[4] or ""))
            ]
            if not captions:
                continue
            drawing_rects = [fitz.Rect(item["rect"]) for item in page.get_drawings() if item.get("rect")]
            for caption in captions:
                nearby = [
                    rect
                    for rect in drawing_rects
                    if rect.y1 <= caption.y1
                    and rect.y0 >= caption.y0 - page.rect.height * 0.62
                    and rect.width > 2
                    and rect.height > 2
                ]
                if not nearby:
                    continue
                figure_rect = nearby[0]
                for rect in nearby[1:]:
                    figure_rect |= rect
                if figure_rect.width < page.rect.width * 0.25 or figure_rect.height < page.rect.height * 0.1:
                    continue

                clip = figure_rect | caption
                clip.x0 = max(page.rect.x0, clip.x0 - 18)
                clip.x1 = min(page.rect.x1, clip.x1 + 18)
                clip.y0 = max(page.rect.y0, clip.y0 - 18)
                clip.y1 = min(page.rect.y1, clip.y1 + 10)
                try:
                    pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip, alpha=False)
                    image_bytes = pixmap.tobytes("png")
                except Exception:
                    continue
                digest = hashlib.sha256(image_bytes).hexdigest()
                if digest in seen_content:
                    continue
                seen_content.add(digest)
                candidates.append(
                    {
                        "score": pixmap.width * pixmap.height * 0.9,
                        "page": page_index,
                        "xref": 1_000_000 + page_index * 100 + len(candidates),
                        "bytes": image_bytes,
                    }
                )
        return candidates
