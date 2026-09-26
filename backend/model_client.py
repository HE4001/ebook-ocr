from __future__ import annotations

from typing import Any

from .gemini_client import GeminiClient, GeminiConfig
from .responses_client import ResponsesClient, ResponsesConfig


def create_model_client(
    settings: dict[str, Any], api_key: str, *, context_reuse_enabled: bool | None = None,
) -> ResponsesClient | GeminiClient:
    if context_reuse_enabled is None:
        context_reuse_enabled = settings.get("context_reuse_enabled", False)
    if settings.get("api_protocol", "openai_responses") == "gemini":
        return GeminiClient(GeminiConfig(
            base_url=settings["base_url"],
            models_path=settings["models_path"],
            api_key=api_key,
            timeout_seconds=settings["timeout_seconds"],
            reasoning_effort=settings["reasoning_effort"],
            context_reuse_enabled=context_reuse_enabled,
        ))
    return ResponsesClient(ResponsesConfig(
        base_url=settings["base_url"],
        responses_path=settings["responses_path"],
        api_key=api_key,
        structured_output=settings["structured_output"],
        timeout_seconds=settings["timeout_seconds"],
        reasoning_effort=settings["reasoning_effort"],
        context_reuse_enabled=context_reuse_enabled,
    ))
