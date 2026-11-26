"""Administrative checkpoint resume; stop the backend before running this CLI."""
import json
import argparse
import logging
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.config import get_settings
from app.db import crud
from app.db.session import SessionLocal
from app.services.workflow_service import WorkflowService


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args()
    if shutil.which("systemctl") and subprocess.run(
        ["systemctl", "is-active", "--quiet", "autosci-backend"], check=False,
    ).returncode == 0:
        parser.error("Stop autosci-backend before resuming a checkpoint in this process")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with SessionLocal() as db:
        crud.recover_interrupted_work(db)
    result = WorkflowService(get_settings().model_copy(deep=True)).retry_quality_revisions(manual=True, run_id=args.run_id)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
