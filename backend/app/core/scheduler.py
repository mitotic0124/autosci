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
        )
        self._scheduler.add_job(
            self._workflow_service.poll_publish_commands,
            trigger=IntervalTrigger(seconds=self._settings.command_poll_interval_seconds),
            id="poll_publish_commands",
            replace_existing=True,
        )
        self._scheduler.start()

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
            )

    def repoll(self, seconds: int) -> None:
        self._settings.command_poll_interval_seconds = seconds
        trigger = IntervalTrigger(seconds=seconds)
        try:
            self._scheduler.reschedule_job("poll_publish_commands", trigger=trigger)
        except JobLookupError:
            self._scheduler.add_job(
                self._workflow_service.poll_publish_commands,
                trigger=trigger,
                id="poll_publish_commands",
                replace_existing=True,
            )
