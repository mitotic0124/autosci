"""Validate and persist Kimi configuration without displaying credential values."""
import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from dotenv import set_key
from app.core.config import get_settings
from app.db import crud
from app.db.session import SessionLocal, engine
from app.services.llm_service import LLMService


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--base-url", default="https://api.kimi.com/coding/v1")
    parser.add_argument("--model", default="k3.1-pro")
    parser.add_argument("--production", action="store_true")
    args = parser.parse_args()
    key = args.key_file.read_text(encoding="utf-8-sig").strip()
    if not key or any(character.isspace() for character in key):
        raise ValueError("Key file must contain one API key")
    values = {
        "llm_provider": "openai_compatible", "llm_api_base_url": args.base_url, "llm_api_key": key,
        "llm_model": args.model, "llm_article_model": args.model, "llm_vision_model": args.model,
        "llm_verifier_model": args.model, "openai_model": args.model,
        "llm_reasoning_effort": "high", "llm_max_output_tokens": "16384",
        "llm_request_timeout_seconds": "300", "llm_use_responses_api": "false",
    }
    settings = get_settings().model_copy(deep=True)
    settings.llm_api_base_url = args.base_url
    settings.llm_api_key = key
    settings.llm_model = args.model
    settings.llm_reasoning_effort = "low"
    settings.llm_max_output_tokens = 2048
    result = LLMService(settings).generate_json("Return valid JSON only.", 'Return {"ok": true}.')
    if not result or result.get("ok") is not True:
        raise RuntimeError("Kimi validation failed; saved configuration has not been changed")
    if args.production:
        values.update(schedule_hour="7", schedule_minute="0", timezone="Asia/Shanghai",
                      workflow_auto_publish_after_run="true", zhihu_auto_publish="true",
                      zhihu_publish_enabled="true", zhihu_headless="true")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = Path("backups") / f"kimi-config-{stamp}"
    backup.mkdir(parents=True, exist_ok=False, mode=0o700)
    env = Path(".env")
    if env.exists():
        shutil.copy2(env, backup / "backend.env")
        (backup / "backend.env").chmod(0o600)
    if engine.url.get_backend_name() == "sqlite":
        with sqlite3.connect(engine.url.database) as source, sqlite3.connect(backup / "settings-before.db") as target:
            source.backup(target)
        (backup / "settings-before.db").chmod(0o600)
    with SessionLocal() as db:
        crud.upsert_settings(db, values)
    for name, value in values.items():
        set_key(str(env), name.upper(), value)
    env.chmod(0o600)
    print(json.dumps({"validated": True, "base_url": args.base_url, "models": [args.model],
                      "all_four_roles": True, "production": args.production, "backup": str(backup)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
