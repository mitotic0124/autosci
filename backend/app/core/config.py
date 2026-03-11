from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_NOTE_SYSTEM_PROMPT = (
    "你是资深量化研究员。请输出结构化、可执行、证据导向的中文分析，"
    "重点覆盖方法论、数据说明和实证结论。"
)

DEFAULT_SCORE_SYSTEM_PROMPT = (
    "你是量化研究负责人。请仅输出 JSON 评分结果，不要添加解释。"
)


class Settings(BaseSettings):
    app_name: str = "AutoSci Quant AI Workflow"
    timezone: str = "Asia/Shanghai"
    database_url: str = "sqlite:///./autosci.db"
    artifacts_dir: str = "artifacts"

    arxiv_base_url: str = "http://export.arxiv.org/api/query"
    arxiv_max_candidates: int = 40
    paper_count: int = 3
    paper_history_lookback_days: int = 7
    arxiv_extra_keywords: str = (
        "volatility,liquidity,market microstructure,limit order book,"
        "transaction cost,return predictability,cross-sectional,alpha"
    )
    schedule_hour: int = 6
    schedule_minute: int = 0

    # Deprecated but kept for backward compatibility.
    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"

    # LLM provider (OpenAI-compatible API).
    llm_provider: str = "openai_compatible"
    llm_api_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str = "gpt-4.1-mini"
    llm_note_system_prompt: str = DEFAULT_NOTE_SYSTEM_PROMPT
    llm_score_system_prompt: str = DEFAULT_SCORE_SYSTEM_PROMPT

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = True
    mail_from: str | None = None
    mail_to: str | None = None

    imap_host: str | None = None
    imap_port: int = 993
    imap_username: str | None = None
    imap_password: str | None = None
    imap_use_ssl: bool = True
    imap_folder: str = "INBOX"
    command_poll_interval_seconds: int = 120
    command_authorized_sender: str | None = None

    zhihu_column_url: str | None = None
    zhihu_firefox_profile_path: str | None = None
    zhihu_headless: bool = False
    zhihu_publish_enabled: bool = True
    zhihu_auto_publish: bool = False
    workflow_auto_publish_after_run: bool = False
    zhihu_markdown_converter_cmd: str = "md2zhihu"

    frontend_origin: str = "http://localhost:5173"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
