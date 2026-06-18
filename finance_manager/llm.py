from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

try:
    from litellm import completion
except Exception:  # pragma: no cover - litellm is optional at runtime
    completion = None

from finance_manager.llm_config import ResolvedLLM, get_llm_config
from finance_manager.logger import logger


def resolve_llm(provider: Optional[str] = None) -> ResolvedLLM:
    """Return the effective LLM settings (provider, model, params)."""
    return get_llm_config().resolve(provider)


def _completion_kwargs(resolved: ResolvedLLM) -> Dict[str, Any]:
    # drop_params lets liteLLM silently drop settings a given model does not
    # accept (for example temperature on Opus / Fable models), so switching
    # providers in the config file never raises a parameter error.
    kwargs: Dict[str, Any] = {"model": resolved.model, "drop_params": True}
    kwargs.update(resolved.params)
    if resolved.api_key_env:
        api_key = os.getenv(resolved.api_key_env)
        if api_key:
            kwargs["api_key"] = api_key
    return kwargs


def complete(prompt: str, provider: Optional[str] = None) -> Optional[str]:
    """Run a single-prompt completion against the active provider.

    Returns the response text, or None when liteLLM is unavailable or the call
    fails. LLM calls are best-effort across the pipeline, so callers should
    handle a None return.
    """
    if completion is None:
        return None
    resolved = resolve_llm(provider)
    messages: List[Dict[str, str]] = [{"role": "user", "content": prompt}]
    try:
        resp = completion(messages=messages, **_completion_kwargs(resolved))
        return resp["choices"][0]["message"]["content"]
    except Exception as err:  # pragma: no cover - LLM is best-effort
        logger.warning(
            "llm_failed", provider=resolved.provider, model=resolved.model, error=str(err)
        )
        return None
