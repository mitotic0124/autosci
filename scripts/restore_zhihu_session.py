"""Transfer only Zhihu cookies and verify the server editor before saving them."""
import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


def export_cookies(profile: Path, output: Path) -> None:
    uri = (profile / "cookies.sqlite").resolve().as_uri() + "?mode=ro"
    cookies = []
    with sqlite3.connect(uri, uri=True) as db:
        for host, name, value, path, expiry, secure, http_only, same_site in db.execute(
            "SELECT host,name,value,path,expiry,isSecure,isHttpOnly,sameSite FROM moz_cookies "
            "WHERE host='zhihu.com' OR host LIKE '%.zhihu.com'"
        ):
            expiry = expiry / 1000 if expiry > 100_000_000_000 else expiry
            if expiry > 0 and expiry <= time.time():
                continue
            cookie = {"domain": host, "name": name, "value": value, "path": path,
                      "secure": bool(secure), "httpOnly": bool(http_only)}
            if expiry > 0:
                cookie["expires"] = expiry
            if same_site in (1, 2, 3):
                cookie["sameSite"] = {1: "Lax", 2: "Strict", 3: "None"}[same_site]
            cookies.append(cookie)
    if not any(cookie["name"] == "z_c0" for cookie in cookies):
        raise RuntimeError("No unexpired Zhihu login cookie in the configured profile")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(cookies), encoding="utf-8")
    os.chmod(output, 0o600)
    print(json.dumps({"exported_zhihu_cookies": len(cookies)}))


def probe_session(cookie_file: Path | None, save: bool) -> None:
    from playwright.sync_api import sync_playwright
    from app.core.config import get_settings
    from app.db.session import SessionLocal
    from app.services.workflow_service import WorkflowService
    from app.services.zhihu_service import ZhihuService

    settings = get_settings().model_copy(deep=True)
    with SessionLocal() as db:
        WorkflowService(settings)._apply_runtime_settings(db)
    service = ZhihuService(settings)
    original = service._resolve_firefox_profile_path()
    if original is None:
        raise RuntimeError("Configured server profile does not exist")
    temporary = service._prepare_launch_profile(original)
    try:
        with sync_playwright() as pw:
            context = pw.firefox.launch_persistent_context(str(temporary), headless=True)
            try:
                if cookie_file:
                    cookies = json.loads(cookie_file.read_text(encoding="utf-8"))
                    if any(cookie["domain"].lstrip(".") != "zhihu.com" and not cookie["domain"].endswith(".zhihu.com") for cookie in cookies):
                        raise ValueError("Refusing non-Zhihu cookies")
                    context.add_cookies(cookies)
                elif hasattr(service, "_restore_managed_session"):
                    service._restore_managed_session(context, original)
                page = context.new_page()
                response = page.goto("https://zhuanlan.zhihu.com/write", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                service._check_access_block(page)
                editor = page.locator("[contenteditable=true]")
                signed_in = "signin" not in page.url and "login" not in page.url and editor.count() > 0
                print(json.dumps({"http_status": response.status if response else None, "url": page.url,
                                  "signed_in": signed_in, "session_saved": signed_in and save}), flush=True)
                if not signed_in:
                    raise RuntimeError("Zhihu session is not valid on the server")
                if save:
                    if not service._save_managed_session(context, original):
                        raise RuntimeError("Could not save the verified session")
            finally:
                context.close()
    finally:
        service._cleanup_launch_profile(temporary, original)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--export-profile", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cookies", type=Path)
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()
    if args.export_profile:
        if args.output is None:
            parser.error("--output is required for export")
        export_cookies(args.export_profile, args.output)
    else:
        probe_session(args.cookies, args.save)


if __name__ == "__main__":
    main()
