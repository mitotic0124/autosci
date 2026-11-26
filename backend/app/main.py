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
    with SessionLocal() as db:
        runtime = crud.list_settings(db)
        workflow_service._apply_settings_map(settings, runtime)
        recovery = crud.recover_interrupted_work(db)
        logging.getLogger(__name__).info("Startup recovery: %s", recovery)
        backfill = workflow_service.backfill_quality_revisions(db)
        if backfill.get("queued"):
            logging.getLogger(__name__).info("Recovered %s rejected article checkpoint(s) for automatic revision.", backfill["queued"])

    scheduler_service.start()


@app.on_event("shutdown")
def on_shutdown() -> None:
    scheduler_service: SchedulerService = app.state.scheduler
    scheduler_service.shutdown()
