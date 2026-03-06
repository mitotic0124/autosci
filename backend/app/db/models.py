from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trigger_source: Mapped[str] = mapped_column(String(32), default="scheduler")
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    papers: Mapped[list["Paper"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    backtest_reports: Mapped[list["BacktestReport"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )
    publication_tasks: Mapped[list["PublicationTask"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )


class Paper(Base):
    __tablename__ = "papers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    arxiv_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(Text)
    authors: Mapped[str] = mapped_column(Text)
    abstract: Mapped[str] = mapped_column(Text)
    categories: Mapped[str] = mapped_column(String(256), default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    url: Mapped[str] = mapped_column(Text)
    pdf_url: Mapped[str] = mapped_column(Text)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    run: Mapped["WorkflowRun"] = relationship(back_populates="papers")
    note: Mapped["PaperNote"] = relationship(back_populates="paper", uselist=False, cascade="all, delete-orphan")


class PaperNote(Base):
    __tablename__ = "paper_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id"), unique=True)
    markdown_content: Mapped[str] = mapped_column(Text)
    image_paths: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    paper: Mapped["Paper"] = relationship(back_populates="note")


class BacktestReport(Base):
    __tablename__ = "backtest_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    paper_id: Mapped[int | None] = mapped_column(ForeignKey("papers.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(256))
    markdown_content: Mapped[str] = mapped_column(Text)
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    artifact_paths: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    run: Mapped["WorkflowRun"] = relationship(back_populates="backtest_reports")
    paper: Mapped["Paper"] = relationship()


class AppSetting(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PublicationTask(Base):
    __tablename__ = "publication_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id"), unique=True, index=True)
    paper_code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    title: Mapped[str] = mapped_column(Text)
    note_markdown: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="PENDING")
    command_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String(256), nullable=True)
    zhihu_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    run: Mapped["WorkflowRun"] = relationship(back_populates="publication_tasks")
    paper: Mapped["Paper"] = relationship()


class EmailCommandLog(Base):
    __tablename__ = "email_command_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_uid: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    sender: Mapped[str] = mapped_column(String(256))
    subject: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    command_text: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="RECEIVED")
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
