from __future__ import annotations

from typing import Any

from .gemini_client import GeminiClient, GeminiConfig
from .responses_client import ModelServiceError, ResponsesClient, ResponsesConfig


def create_model_client(settings: dict[str, Any], api_key: str) -> ResponsesClient | GeminiClient:
    protocol = settings.get("api_protocol", "openai_responses")
    if protocol not in {"openai_responses", "gemini"}:
        raise ModelServiceError("不支持的模型服务协议", configuration_error=True)
    if protocol == "gemini":
        return GeminiClient(GeminiConfig(
            base_url=settings["base_url"],
            models_path=settings["models_path"],
            api_key=api_key,
            timeout_seconds=settings["timeout_seconds"],
            reasoning_effort=settings["reasoning_effort"],
        ))
    return ResponsesClient(ResponsesConfig(
        base_url=settings["base_url"],
        responses_path=settings["responses_path"],
        api_key=api_key,
        structured_output=settings["structured_output"],
        timeout_seconds=settings["timeout_seconds"],
        reasoning_effort=settings["reasoning_effort"],
    ))
