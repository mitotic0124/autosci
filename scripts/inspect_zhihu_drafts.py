"""Capture a reloaded Zhihu draft for visual acceptance; never click publish."""
import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from playwright.sync_api import sync_playwright
from app.core.config import get_settings
from app.db import crud
from app.db.session import SessionLocal
from app.services.workflow_service import WorkflowService
from app.services.zhihu_service import ZhihuService


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("codes", nargs="+")
    parser.add_argument("--configure", action="store_true", help="Apply topic and column choices without publishing")
    args = parser.parse_args()
    settings = get_settings().model_copy(deep=True)
    with SessionLocal() as db:
        WorkflowService(settings)._apply_runtime_settings(db)
        tasks = [crud.get_publication_task_by_code(db, code) for code in args.codes]
        if any(task is None for task in tasks):
            raise RuntimeError("Unknown publication code")
        targets = []
        for task in tasks:
            directory = Path(settings.artifacts_dir) / "runs" / f"run_{task.run_id}" / "papers" / task.paper.arxiv_id.replace("/", "_")
            document = json.loads((directory / "article_document.json").read_text(encoding="utf-8"))
            targets.append((task.paper_code, task.zhihu_url, task.title, task.note_markdown, document))
    service = ZhihuService(settings)
    source_profile = service._resolve_firefox_profile_path()
    profile = service._prepare_launch_profile(source_profile)
    root = Path(settings.artifacts_dir) / "zhihu_acceptance"
    root.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        context = pw.firefox.launch_persistent_context(str(profile), headless=True, viewport={"width": 1400, "height": 1000})
        try:
            service._restore_managed_session(context, source_profile)
            page = context.new_page()
            for code, url, title, markdown, document in targets:
                if not url:
                    raise RuntimeError(f"{code} has no draft URL")
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                service._check_access_block(page)
                service._dismiss_creator_assistant(page)
                settings_error = None
                if args.configure:
                    try:
                        service._configure_publication_settings(page)
                    except Exception as exc:
                        settings_error = str(exc)
                else:
                    expected_images = sum(block["type"] == "figure" for section in document["sections"] for block in section["blocks"])
                    service._validate_saved_draft(page, url, title, service._strip_leading_h1(markdown), expected_images)
                service._wait_for_math_rendering(page)
                service._wait_for_image_loading(page)
                data = service._read_editor_metrics(page)
                data["settings_error"] = settings_error
                data["settings"] = service._publication_settings_state(page)
                service._assert_publication_settings(page)
                data["url"] = page.url
                data["body"] = page.locator("body").inner_text()
                data["formulas"] = page.locator("[contenteditable] span[data-tex], [contenteditable] img[data-eeimg], [contenteditable] img[eeimg]").evaluate_all("nodes => nodes.map(n => ({tex:n.getAttribute('data-tex') || n.getAttribute('alt'), html:n.outerHTML.slice(0,800)}))")
                data["inputs"] = page.locator("input").evaluate_all("nodes => nodes.map(n => ({type:n.type,placeholder:n.placeholder,aria:n.getAttribute('aria-label'),checked:n.checked}))")
                normalize = lambda value: re.sub(r"\s+|\u200b", "", unicodedata.normalize("NFKC", value))
                editor_text = normalize(data.get("proseText", ""))
                paragraphs = [block for section in document["sections"] for block in section["blocks"] if block["type"] == "paragraph"]
                def prose_parts(text):
                    converted, _ = service._convert_latex_formulas(text)
                    plain = service._rich_html_to_plain_text(service._markdown_to_rich_html(converted))
                    return [normalize(line) for line in plain.splitlines() if normalize(line)]
                data["missingParagraphs"] = [block["id"] for block in paragraphs if any(part not in editor_text for part in prose_parts(block["text"]))]
                data["expectedFormulas"] = service._count_latex_formulas(markdown)
                data["expectedImages"] = sum(block["type"] == "figure" for section in document["sections"] for block in section["blocks"])
                data["title"] = page.locator("textarea[placeholder*='标题'], input[placeholder*='标题']").first.input_value()
                data["titleMatches"] = data["title"] == title
                data["passed"] = (
                    settings_error is None and data["titleMatches"] and not data["missingParagraphs"]
                    and data.get("formulaCount") == data["expectedFormulas"]
                    and data.get("unrenderedFormulaCount") == 0
                    and data.get("malformedFormulaCount") == 0
                    and data.get("imageCount") == data["expectedImages"]
                    and data.get("invalidImageCount") == 0
                )
                (root / f"{code}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                (root / f"{code}.html").write_text(page.content(), encoding="utf-8")
                page.screenshot(path=str(root / f"{code}.png"), full_page=True)
                formulas = page.locator("[contenteditable] span[data-tex], [contenteditable] img[data-eeimg], [contenteditable] img[eeimg]")
                for index in range(formulas.count()):
                    formula = formulas.nth(index)
                    latex = formula.get_attribute("data-tex") or formula.get_attribute("alt") or ""
                    if index < 30 or len(latex) > 90:
                        visual = formula.locator("svg, mjx-container, .katex").first
                        formula.evaluate("node => node.scrollIntoView({block: 'center'})")
                        page.wait_for_timeout(150)
                        (visual if visual.count() else formula).screenshot(path=str(root / f"{code}-formula-{index + 1:02d}.png"))
                print(json.dumps({"code": code, "passed": data["passed"], "missingParagraphs": data["missingParagraphs"], "formula_count": data.get("formulaCount"), "images": data.get("imageCount"), "artifacts": str(root)}, ensure_ascii=False), flush=True)
                if not data["passed"]:
                    raise RuntimeError(f"Draft acceptance failed: {code}")
        finally:
            context.close()
            service._cleanup_launch_profile(profile, source_profile)


if __name__ == "__main__":
    main()
