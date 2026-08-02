from __future__ import annotations

from functools import lru_cache
from typing import List, Literal, Optional

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Central application settings loaded from environment variables."""

    app_name: str = "Agentic Finance Manager"
    environment: str = Field(default="development")
    # LLM provider and model settings live in config/llm.yaml
    # (see finance_manager.llm_config). LLM_MODEL still works as an override
    # of the active provider's model string for backward compatibility.
    llm_model: str = Field(default="gemini-2.5-flash", alias="LLM_MODEL")

    database_url: str = Field(default="postgresql+psycopg://user:pass@localhost:5432/finance", alias="DATABASE_URL")
    qdrant_url: str = Field(default="http://localhost:6333", alias="QDRANT_URL")
    qdrant_api_key: Optional[str] = Field(default=None, alias="QDRANT_API_KEY")
    redis_url: Optional[str] = Field(default=None, alias="REDIS_URL")

    doc_ai_endpoint: Optional[str] = Field(default=None, alias="DOC_AI_ENDPOINT")
    doc_ai_api_key: Optional[str] = Field(default=None, alias="DOC_AI_API_KEY")

    scrapeless_api_key: Optional[str] = Field(default=None, alias="SCRAPELESS_API_KEY")
    playwright_browser: str = Field(default="chromium", alias="PLAYWRIGHT_BROWSER")

    search_mode: Literal["http", "mcp"] = Field(default="http", alias="SEARCH_MODE")
    mcp_server_url: Optional[str] = Field(default=None, alias="MCP_SERVER_URL")
    tavily_api_key: Optional[str] = Field(default=None, alias="TAVILY_API_KEY")

    stt_provider: Literal["whisper", "qwen"] = Field(default="whisper", alias="STT_PROVIDER")
    openai_api_key: Optional[str] = Field(default=None, alias="OPENAI_API_KEY")
    hf_token: Optional[str] = Field(default=None, alias="HF_TOKEN")

    # Email ingestion (IMAP). Mailboxes are opened read-only, so ingestion never
    # marks messages as seen. Use a provider app password, not the account
    # password, where the provider supports one.
    imap_host: Optional[str] = Field(default=None, alias="IMAP_HOST")
    imap_port: int = Field(default=993, alias="IMAP_PORT")
    imap_username: Optional[str] = Field(default=None, alias="IMAP_USERNAME")
    imap_password: Optional[str] = Field(default=None, alias="IMAP_PASSWORD")
    imap_folder: str = Field(default="INBOX", alias="IMAP_FOLDER")
    imap_use_ssl: bool = Field(default=True, alias="IMAP_USE_SSL")
    # How far back a fetch looks, and the cap on messages per fetch.
    email_since_days: int = Field(default=30, alias="EMAIL_SINCE_DAYS")
    email_fetch_limit: int = Field(default=200, alias="EMAIL_FETCH_LIMIT")
    # Addresses or domains allowed to produce transactions. Empty accepts all.
    email_allowed_senders: List[str] = Field(default_factory=list)

    # Budget alerting: fraction of a limit at which a warning is raised.
    budget_warn_threshold: float = Field(default=0.8, alias="BUDGET_WARN_THRESHOLD")
    # Charges needed before a merchant is treated as a recurring series.
    recurring_min_occurrences: int = Field(default=3, alias="RECURRING_MIN_OCCURRENCES")
    # Lookahead window for the "upcoming charges" list, in days.
    recurring_upcoming_days: int = Field(default=14, alias="RECURRING_UPCOMING_DAYS")

    default_currency: str = "USD"
    default_categories: List[str] = Field(
        default_factory=lambda: [
            "Food",
            "Transport",
            "Groceries",
            "Utilities",
            "Rent",
            "Entertainment",
            "Shopping",
            "Healthcare",
            "Other",
        ]
    )
    default_bank_senders: List[str] = Field(default_factory=lambda: ["BOC", "HNB", "COMMERCIAL_BANK", "AMEX"])

    class Config:
        env_file = ".env"
        env_prefix = ""
        extra = "ignore"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()


