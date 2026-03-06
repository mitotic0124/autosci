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

        candidates: list[tuple[int, int, bytes, str]] = []
        seen_xref: set[int] = set()
        try:
            for page in doc:
                for item in page.get_images(full=True):
                    xref = item[0]
                    if xref in seen_xref:
                        continue
                    seen_xref.add(xref)
                    data = doc.extract_image(xref)
                    width = int(data.get("width", 0))
                    height = int(data.get("height", 0))
                    if width * height < 50_000:
                        continue
                    candidates.append((width * height, xref, data["image"], data.get("ext", "png")))
        finally:
            doc.close()

        candidates.sort(key=lambda value: value[0], reverse=True)
        selected = candidates[:max_images]

        outputs: list[Path] = []
        for idx, (_, xref, image_bytes, ext) in enumerate(selected, start=1):
            out = output_dir / f"figure_{idx}_{xref}.{ext}"
            out.write_bytes(image_bytes)
            outputs.append(out)
        return outputs
