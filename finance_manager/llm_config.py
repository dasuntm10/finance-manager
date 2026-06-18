from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from pydantic import BaseModel, Field

from finance_manager.logger import logger


# Resolution order for the LLM config file:
#   1. LLM_CONFIG_FILE environment variable (absolute or relative path)
#   2. <project root>/config/llm.yaml
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "config" / "llm.yaml"


class LLMProviderProfile(BaseModel):
    """A single LLM provider configuration: model id plus call settings."""

    model: str
    api_key_env: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)


class ResolvedLLM(BaseModel):
    """The effective LLM settings after applying environment overrides."""

    provider: str
    model: str
    api_key_env: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)


class LLMConfig(BaseModel):
    """Top-level LLM configuration loaded from config/llm.yaml."""

    active_provider: str
    providers: Dict[str, LLMProviderProfile]

    def resolve(self, provider: Optional[str] = None) -> ResolvedLLM:
        """Resolve the active provider profile, honoring env overrides.

        - LLM_PROVIDER overrides which provider is selected.
        - LLM_MODEL overrides the model string of the selected provider.
        """
        name = provider or os.getenv("LLM_PROVIDER") or self.active_provider
        if name not in self.providers:
            raise KeyError(
                f"LLM provider '{name}' is not defined in the config. "
                f"Available providers: {', '.join(sorted(self.providers))}"
            )
        profile = self.providers[name]
        return ResolvedLLM(
            provider=name,
            model=os.getenv("LLM_MODEL") or profile.model,
            api_key_env=profile.api_key_env,
            params=dict(profile.params),
        )


def _builtin_config() -> LLMConfig:
    """Fallback config used when config/llm.yaml is missing or invalid."""
    return LLMConfig(
        active_provider="gemini",
        providers={
            "gemini": LLMProviderProfile(
                model="gemini/gemini-2.5-flash",
                api_key_env="GEMINI_API_KEY",
                params={"temperature": 0.2, "max_tokens": 1024},
            ),
            "anthropic": LLMProviderProfile(
                model="anthropic/claude-haiku-4-5",
                api_key_env="ANTHROPIC_API_KEY",
                params={"temperature": 0.2, "max_tokens": 1024},
            ),
        },
    )


def _config_path() -> Path:
    override = os.getenv("LLM_CONFIG_FILE")
    return Path(override) if override else _DEFAULT_CONFIG_PATH


@lru_cache(maxsize=1)
def get_llm_config() -> LLMConfig:
    """Load and cache the LLM config from disk, falling back to defaults."""
    path = _config_path()
    if not path.exists():
        logger.warning("llm_config_missing", path=str(path))
        return _builtin_config()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return LLMConfig.model_validate(data)
    except Exception as err:  # pragma: no cover - config errors are best-effort
        logger.warning("llm_config_invalid", path=str(path), error=str(err))
        return _builtin_config()
