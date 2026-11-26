from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_NOTE_SYSTEM_PROMPT = (
    "你是资深量化研究员。请使用准确、克制、证据导向的中文表达，"
    "严格区分论文原文结论与编辑解释，不补充未提供的事实、数字、建议或未来研究方向。"
)

DEFAULT_SCORE_SYSTEM_PROMPT = (
    "你是量化研究负责人。请仅输出 JSON 评分结果，不要添加解释。"
)

DEFAULT_DEEPSEEK_MODEL = "deepseek-ai/DeepSeek-V4-Flash"
DEFAULT_KIMI_MODEL = "k3.1-pro"

SECRET_SETTING_KEYS = frozenset({"llm_api_key", "smtp_password", "imap_password"})


class Settings(BaseSettings):
    app_name: str = "AutoSci Quant AI Workflow"
    timezone: str = "Asia/Shanghai"
    database_url: str = "sqlite:///./autosci.db"
    artifacts_dir: str = "artifacts"

    arxiv_base_url: str = "https://export.arxiv.org/api/query"
    arxiv_max_candidates: int = 40
    # arXiv asks clients to identify themselves and keep requests paced.
    # A stable user agent also avoids the generic 429 responses seen from
    # unidentified API clients.
    arxiv_user_agent: str = "AutoSci/1.0"
    paper_count: int = 3
    paper_history_lookback_days: int = 7
    arxiv_extra_keywords: str = (
        "volatility,liquidity,market microstructure,limit order book,"
        "transaction cost,return predictability,cross-sectional,alpha"
    )
    schedule_hour: int = 7
    schedule_minute: int = 0

    # Deprecated but kept for backward compatibility.
    openai_api_key: str | None = None
    openai_model: str = DEFAULT_KIMI_MODEL

    # LLM provider (OpenAI-compatible API).
    llm_provider: str = "openai_compatible"
    llm_api_base_url: str | None = "https://api.kimi.com/coding/v1"
    llm_api_key: str | None = None
    llm_model: str = DEFAULT_KIMI_MODEL
    llm_article_model: str | None = DEFAULT_KIMI_MODEL
    llm_vision_model: str | None = DEFAULT_KIMI_MODEL
    llm_verifier_model: str | None = DEFAULT_KIMI_MODEL
    llm_request_timeout_seconds: int = 300
    llm_max_output_tokens: int = 16384
    llm_thinking_budget: int = 2048
    llm_reasoning_effort: str = "high"
    llm_use_responses_api: bool = False
    llm_note_system_prompt: str = DEFAULT_NOTE_SYSTEM_PROMPT
    llm_score_system_prompt: str = DEFAULT_SCORE_SYSTEM_PROMPT

    # Evidence-grounded article pipeline.
    article_fetch_arxiv_source: bool = True
    article_max_source_chars: int = 70000
    article_max_claims: int = 42
    article_max_figure_candidates: int = 12
    article_min_images: int = 1
    article_max_images: int = 4
    article_min_figure_score: float = 0.56
    article_quality_threshold: float = 0.90
    article_max_repair_attempts: int = 2
    article_max_quality_retries: int = 2
    article_max_quality_candidates: int = 5
    article_parallel_workers: int = 3
    article_require_verified: bool = True
    article_min_paragraphs: int = 8
    article_min_sections: int = 4
    article_min_chars: int = 1800

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
    zhihu_column_name: str = "量化研究速递"
    # These are verified to exist in the account's current Zhihu topic picker.
    # The current account's topic picker exposes "投资" reliably; using a
    # non-existent exact topic blocks the entire publication before the
    # publish request is sent.
    zhihu_default_tags: str = "投资"
    zhihu_topic_api_enabled: bool = True
    zhihu_topic_max_count: int = 2
    zhihu_topic_min_relevance: float = 0.45
    zhihu_firefox_profile_path: str | None = None
    zhihu_headless: bool = False
    zhihu_publish_enabled: bool = True
    zhihu_auto_publish: bool = False
    zhihu_max_publications_per_day: int = 1
    zhihu_min_interaction_interval_seconds: int = 1800
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
