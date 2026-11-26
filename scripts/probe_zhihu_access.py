"""One read-only probe of the writer, one observed article API, and its public page."""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from playwright.sync_api import sync_playwright
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.workflow_service import WorkflowService
from app.services.zhihu_service import ZhihuService


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--article-id", required=True)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"\d+", args.article_id):
        raise ValueError("An observed article ID is required")
    settings = get_settings().model_copy(deep=True)
    with SessionLocal() as db:
        WorkflowService(settings)._apply_runtime_settings(db)
    service = ZhihuService(settings)
    source = service._resolve_firefox_profile_path()
    profile = service._prepare_launch_profile(source)
    try:
        with sync_playwright() as pw:
            context = pw.firefox.launch_persistent_context(str(profile), headless=not args.headed)
            try:
                service._restore_managed_session(context, source)
                page = context.new_page()
                r = page.goto("https://zhuanlan.zhihu.com/write", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2500)
                print(json.dumps({"route": "writer", "status": r.status, "url": page.url,
                                  "editor": page.locator("[contenteditable=true]").count()}, ensure_ascii=False), flush=True)
                api = context.request.get(f"https://zhuanlan.zhihu.com/api/articles/{args.article_id}", timeout=30000)
                info = {"route": "article_api", "status": api.status}
                if api.ok:
                    data = api.json()
                    info.update({key: data[key] for key in ("id", "title", "isPublished", "is_published", "state", "url") if key in data})
                    info["topics"] = [item.get("name") for item in data.get("topics", [])]
                    info["column"] = data.get("column")
                    if args.output:
                        args.output.write_text(json.dumps({key: data.get(key) for key in ("id", "title", "state", "url", "content", "topics", "column")}, ensure_ascii=False), encoding="utf-8")
                    info["keys"] = list(data)[:30]
                    info["content_chars"] = len(data.get("content") or "")
                print(json.dumps(info, ensure_ascii=False), flush=True)
                r = page.goto(f"https://zhuanlan.zhihu.com/p/{args.article_id}", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2500)
                print(json.dumps({"route": "public", "status": r.status, "url": page.url,
                                  "article": page.locator(".Post-RichTextContainer, .Post-RichText, article .RichText").count()}, ensure_ascii=False), flush=True)
            finally:
                context.close()
    finally:
        service._cleanup_launch_profile(profile, source)


if __name__ == "__main__":
    main()
