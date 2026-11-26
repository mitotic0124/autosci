import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.config import get_settings
from app.db import crud
from app.db.session import SessionLocal


def main() -> None:
    with SessionLocal() as db:
        current = crud.list_settings(db).get("zhihu_default_tags")
        crud.upsert_settings(db, {"zhihu_default_tags": "投资"})
        print({"previous": current, "updated": "投资"})


if __name__ == "__main__":
    main()
