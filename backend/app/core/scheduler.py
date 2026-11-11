from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.base import JobLookupError
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import Settings


class SchedulerService:
    def __init__(self, settings: Settings, workflow_service) -> None:
        self._settings = settings
        self._workflow_service = workflow_service
        self._scheduler = BackgroundScheduler(timezone=ZoneInfo(settings.timezone))

    def start(self) -> None:
        self._scheduler.add_job(
            self._workflow_service.run_pipeline,
            trigger=CronTrigger(
                hour=self._settings.schedule_hour,
                minute=self._settings.schedule_minute,
                timezone=ZoneInfo(self._settings.timezone),
            ),
            id="daily_quant_pipeline",
            replace_existing=True,
            kwargs={"trigger_source": "scheduler"},
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
        # The cron fire is only a wakeup. Durable history determines whether
        # today's work ran, including after downtime or a busy workflow lock.
        self._scheduler.add_job(
            self._workflow_service.run_pipeline,
            trigger=IntervalTrigger(minutes=1),
            id="daily_workflow_catchup",
            kwargs={"trigger_source": "scheduler"},
            next_run_time=datetime.now(timezone.utc),
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60,
        )
        self._scheduler.add_job(
            self._workflow_service.poll_publish_commands,
            trigger=IntervalTrigger(
                seconds=self._settings.command_poll_interval_seconds,
                start_date=datetime.now(timezone.utc) + timedelta(seconds=10),
            ),
            id="poll_publish_commands",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self._scheduler.add_job(
            self._workflow_service.retry_automatic_publications,
            trigger=IntervalTrigger(
                minutes=15,
                start_date=datetime.now(timezone.utc) + timedelta(seconds=20),
            ),
            id="retry_automatic_publications",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self._scheduler.add_job(
            self._workflow_service.retry_quality_revisions,
            trigger=IntervalTrigger(
                minutes=15,
                start_date=datetime.now(timezone.utc) + timedelta(minutes=5, seconds=40),
            ),
            id="retry_quality_revisions",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self._scheduler.start()

    def status(self) -> dict:
        return {
            "running": self._scheduler.running,
            "timezone": self._settings.timezone,
            "jobs": [
                {"id": job.id, "next_run_at": job.next_run_time.isoformat() if job.next_run_time else None}
                for job in self._scheduler.get_jobs()
            ],
        }

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def reschedule(self, hour: int, minute: int) -> None:
        self._settings.schedule_hour = hour
        self._settings.schedule_minute = minute
        trigger = CronTrigger(hour=hour, minute=minute, timezone=ZoneInfo(self._settings.timezone))
        try:
            self._scheduler.reschedule_job("daily_quant_pipeline", trigger=trigger)
        except JobLookupError:
            self._scheduler.add_job(
                self._workflow_service.run_pipeline,
                trigger=trigger,
                id="daily_quant_pipeline",
                replace_existing=True,
                kwargs={"trigger_source": "scheduler"},
                coalesce=True,
                max_instances=1,
                misfire_grace_time=3600,
            )

    def repoll(self, seconds: int) -> None:
        self._settings.command_poll_interval_seconds = seconds
        trigger = IntervalTrigger(
            seconds=seconds,
            start_date=datetime.now(timezone.utc) + timedelta(seconds=10),
        )
        try:
            self._scheduler.reschedule_job("poll_publish_commands", trigger=trigger)
        except JobLookupError:
            self._scheduler.add_job(
                self._workflow_service.poll_publish_commands,
                trigger=trigger,
                id="poll_publish_commands",
                replace_existing=True,
            )
