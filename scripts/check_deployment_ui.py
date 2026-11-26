"""Read-only acceptance of the deployed console and scheduler state."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--output", type=Path, default=Path("artifacts/deployment-acceptance"))
    parser.add_argument("--expect-public", action="store_true")
    parser.add_argument("--run-id", type=int)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.firefox.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(args.url, wait_until="networkidle", timeout=30000)
            page.get_by_text("\u670d\u52a1\u4e0e\u5b9a\u65f6\u8c03\u5ea6\u8fd0\u884c\u4e2d", exact=True).wait_for(timeout=10000)
            health = page.request.get(f"{args.url}/health").json()
            assert health["status"] == "ok", health
            assert health["scheduler"]["running"] is True, health
            assert health["public_publication"] is args.expect_public, health
            assert health["automatic_publication"] is args.expect_public, health
            jobs = {job["id"]: job for job in health["scheduler"]["jobs"]}
            for job_id in ("daily_quant_pipeline", "retry_automatic_publications"):
                assert jobs[job_id]["next_run_at"], jobs
            if args.expect_public:
                page.get_by_text("\u5141\u8bb8\u516c\u5f00\u53d1\u5e03", exact=True).wait_for()
                page.get_by_text("\u81ea\u52a8\u53d1\u5e03\u5df2\u542f\u7528", exact=False).wait_for()
            run = None
            if args.run_id:
                response = page.request.get(f"{args.url}/api/workflow/runs/{args.run_id}")
                assert response.ok, response.status
                run = response.json()
            page.screenshot(path=str(args.output / "console-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(args.output / "console-mobile.png"), full_page=True)
            overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
            payload = {"health": health, "page_errors": errors, "title": page.title(),
                       "mobile_overflow": overflow, "run_id": args.run_id,
                       "run_status": run.get("run", {}).get("status") if run else None}
            (args.output / "verification.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(payload, ensure_ascii=False))
            assert not errors, errors
            assert not overflow, "Mobile console overflows the viewport"
        finally:
            browser.close()


if __name__ == "__main__":
    main()
