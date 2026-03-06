from datetime import datetime

from pydantic import BaseModel


class WorkflowRunOut(BaseModel):
    id: int
    trigger_source: str
    status: str
    started_at: datetime
    ended_at: datetime | None
    summary: str | None
    error_message: str | None

    model_config = {"from_attributes": True}


class PaperOut(BaseModel):
    id: int
    run_id: int
    arxiv_id: str
    title: str
    authors: str
    abstract: str
    categories: str
    published_at: datetime | None
    url: str
    pdf_url: str
    score: float

    model_config = {"from_attributes": True}


class PaperNoteOut(BaseModel):
    paper_id: int
    markdown_content: str
    image_paths: str

    model_config = {"from_attributes": True}


class BacktestReportOut(BaseModel):
    id: int
    run_id: int
    paper_id: int | None
    title: str
    markdown_content: str
    metrics_json: str
    artifact_paths: str

    model_config = {"from_attributes": True}


class PublicationTaskOut(BaseModel):
    id: int
    run_id: int
    paper_id: int
    paper_code: str
    title: str
    status: str
    requested_by: str | None
    zhihu_url: str | None
    error_message: str | None
    published_at: datetime | None

    model_config = {"from_attributes": True}


class TriggerResponse(BaseModel):
    accepted: bool
    message: str
