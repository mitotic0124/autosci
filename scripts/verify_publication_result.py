"""Read-only public-page verification of an uncertain publication, never submit."""
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
    parser.add_argument("--task-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--official-record", action="store_true")
    args = parser.parse_args()
    task = json.loads(args.task_file.read_text(encoding="utf-8"))
    url = str(task["url"]).removesuffix("/edit")
    if not re.fullmatch(r"https://zhuanlan\.zhihu\.com/p/\d+", url):
        raise ValueError("Expected an observed Zhihu article URL")
    settings = get_settings().model_copy(deep=True)
    with SessionLocal() as db:
        WorkflowService(settings)._apply_runtime_settings(db)
    service = ZhihuService(settings)
    source_profile = service._resolve_firefox_profile_path()
    profile = service._prepare_launch_profile(source_profile)
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as pw:
            context = pw.firefox.launch_persistent_context(str(profile), headless=True, viewport={"width": 1400, "height": 1000})
            try:
                service._restore_managed_session(context, source_profile)
                page = context.new_page()
                if args.official_record:
                    verified_url = service._verify_published_api(page, url.rsplit("/", 1)[-1], task["title"],
                        service._strip_leading_h1(task["markdown"]), task["image_count"])
                else:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(3000)
                    verified_url = service._verify_published_url(page, expected_title=task["title"],
                        source_body=service._strip_leading_h1(task["markdown"]), expected_images=task["image_count"])
                page.screenshot(path=str(args.output / "public-top.png"))
                (args.output / "public.html").write_text(page.content(), encoding="utf-8")
                result = {"verified": True, "url": verified_url, "title": task["title"],
                          "image_count": task["image_count"], "expected_equations": service._count_latex_formulas(task["markdown"]),
                          "verification_mode": service._load_zhihu_state().get("public_readback_mode", "public_html")}
                (args.output / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps(result, ensure_ascii=False), flush=True)
            finally:
                context.close()
    finally:
        service._cleanup_launch_profile(profile, source_profile)


if __name__ == "__main__":
    main()
