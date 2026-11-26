"""Read public articles and record visible content, media, topics and column links."""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from playwright.sync_api import sync_playwright
from app.core.config import get_settings
from app.db import crud
from app.db.session import SessionLocal
from app.services.workflow_service import WorkflowService
from app.services.zhihu_service import ZhihuService


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("codes", nargs="+")
    args = parser.parse_args()
    settings = get_settings().model_copy(deep=True)
    with SessionLocal() as db:
        WorkflowService(settings)._apply_runtime_settings(db)
        tasks = [crud.get_publication_task_by_code(db, code) for code in args.codes]
        if any(task is None or task.status != "PUBLISHED" for task in tasks):
            raise RuntimeError("All targets must have a confirmed published status")
        targets = [(task.paper_code, task.zhihu_url, task.title, task.note_markdown) for task in tasks]
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
            for code, url, title, markdown in targets:
                response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                service._check_access_block(page)
                article = page.locator(".Post-RichTextContainer, .Post-RichText, article .RichText").first
                article.wait_for(timeout=15000)
                media = article.locator("span[data-tex], img")
                for index in range(media.count()):
                    media.nth(index).scroll_into_view_if_needed()
                    page.wait_for_timeout(200)
                page.wait_for_function("""() => {
                    const root = document.querySelector('.Post-RichTextContainer, .Post-RichText, article .RichText');
                    return root && Array.from(root.querySelectorAll('img')).every(n => n.complete && n.naturalWidth > 0);
                }""", timeout=20000)
                data = article.evaluate("""root => {
                    const fs = Array.from(root.querySelectorAll('span[data-tex], img[data-eeimg], img[eeimg], img[src*=\"equation?tex=\"]'));
                    const images = Array.from(root.querySelectorAll('img')).filter(n => !fs.includes(n));
                    const prose = root.cloneNode(true);
                    prose.querySelectorAll('span[data-tex], img').forEach(n => n.remove());
                    return {text: root.innerText, proseText: prose.textContent, formulaCount: fs.length,
                        formulaTexts: fs.map(n => n.getAttribute('data-tex') || n.getAttribute('alt') || ''),
                        unrenderedFormulaCount: fs.filter(n => n.tagName === 'IMG' ? !n.complete || !n.naturalWidth : !n.querySelector('svg, math, mjx-container, .katex') || n.querySelector('.MathJax_Error, .merror, merror, .MJXp-merror, .katex-error, [data-mjx-error]') || Array.from(n.querySelectorAll('mtext[mathcolor="red"]')).some(x => /^\\\\[A-Za-z]/.test(x.textContent || ''))).length,
                        imageCount: images.length, invalidImageCount: images.filter(n => !n.complete || !n.naturalWidth).length,
                        headingCount: root.querySelectorAll('h1,h2,h3,h4').length};
                }""")
                data.update({"url": page.url, "httpStatus": response.status, "title": page.locator("h1").first.inner_text(), "pageText": page.locator("body").inner_text(), "hasEditor": page.locator(".WriteIndex [contenteditable=true]").count() > 0})
                service._assert_public_article_state(data, title)
                service._assert_source_content(data, service._strip_leading_h1(markdown))
                data["topicLinks"] = page.locator("a[href*='/topic/']").evaluate_all("nodes => nodes.map(n => ({text:n.innerText, href:n.href}))")
                data["columnLinks"] = page.locator("a").evaluate_all("(nodes, column) => nodes.filter(n => n.innerText.trim() === column).map(n => ({text:n.innerText, href:n.href}))", settings.zhihu_column_name)
                data["expectedFormulas"] = service._count_latex_formulas(markdown)
                data["expectedImages"] = len(re.findall(r"!\[[^\]]*\]\(", markdown))
                data["passed"] = data["formulaCount"] == data["expectedFormulas"] and data["unrenderedFormulaCount"] == 0 and data["imageCount"] == data["expectedImages"] and data["invalidImageCount"] == 0 and bool(data["columnLinks"]) and all(tag.strip() in [item["text"].strip() for item in data["topicLinks"]] for tag in settings.zhihu_default_tags.split(",") if tag.strip())
                (root / f"{code}-public.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                (root / f"{code}-public.html").write_text(page.content(), encoding="utf-8")
                page.screenshot(path=str(root / f"{code}-public.png"), full_page=True)
                page.evaluate("window.scrollTo(0, 0)")
                page.screenshot(path=str(root / f"{code}-public-top.png"))
                print(json.dumps({"code": code, **{key: data[key] for key in ("passed", "url", "formulaCount", "unrenderedFormulaCount", "imageCount", "invalidImageCount", "topicLinks", "columnLinks")}}, ensure_ascii=False), flush=True)
                if not data["passed"]:
                    raise RuntimeError(f"Public article acceptance failed: {code}")
        finally:
            context.close()
            service._cleanup_launch_profile(profile, source_profile)


if __name__ == "__main__":
    main()
