import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.config import get_settings
from app.core.scheduler import SchedulerService
from app.db import crud
from app.db.models import Base
from app.db.session import SessionLocal, engine
from app.services.workflow_service import WorkflowService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

settings = get_settings()
app = FastAPI(title=settings.app_name)

workflow_service = WorkflowService(settings)
scheduler_service = SchedulerService(settings, workflow_service)
app.state.workflow_service = workflow_service
app.state.scheduler = scheduler_service

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin, "http://localhost:8080"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

artifacts_dir = Path(settings.artifacts_dir)
artifacts_dir.mkdir(parents=True, exist_ok=True)
app.mount("/artifacts", StaticFiles(directory=artifacts_dir), name="artifacts")


@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        runtime = crud.list_settings(db)
    finally:
        db.close()

    int_fields = {
        "schedule_hour",
        "schedule_minute",
        "paper_count",
        "arxiv_max_candidates",
        "smtp_port",
        "imap_port",
        "command_poll_interval_seconds",
    }
    bool_fields = {"smtp_use_tls", "imap_use_ssl", "zhihu_headless", "zhihu_publish_enabled"}
    for key, value in runtime.items():
        if not hasattr(settings, key):
            continue
        if key in int_fields:
            cast_value = int(value)
        elif key in bool_fields:
            cast_value = value.lower() in {"1", "true", "yes", "on"}
        else:
            cast_value = value
        setattr(settings, key, cast_value)

    scheduler_service.start()


@app.on_event("shutdown")
def on_shutdown() -> None:
    scheduler_service: SchedulerService = app.state.scheduler
    scheduler_service.shutdown()
