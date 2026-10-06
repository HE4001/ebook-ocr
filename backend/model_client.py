from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from .gemini_client import GeminiClient, GeminiConfig
from .responses_client import (
    ContentRequestResult, ModelServiceError, OrderRequestResult, ResponsesClient,
    ResponsesConfig, ReviewRequestResult,
)


def create_model_client(settings: dict[str, Any], api_key: str) -> ResponsesClient | GeminiClient:
    protocol = settings.get("api_protocol", "openai_responses")
    if protocol not in {"openai_responses", "gemini"}:
        raise ModelServiceError("不支持的模型服务协议", configuration_error=True,
                                reason_category="service_configuration", field_path="api_protocol")
    if not api_key.strip() or "\n" in api_key or "\r" in api_key:
        raise ModelServiceError("模型服务密钥未配置或格式无效", configuration_error=True,
                                reason_category="service_configuration", field_path="api_key")
    base_url = settings.get("base_url")
    if not isinstance(base_url, str):
        raise ModelServiceError("模型服务地址未配置", configuration_error=True,
                                reason_category="service_configuration", field_path="base_url")
    try:
        parsed = urlsplit(base_url)
        valid_url = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
        valid_url = valid_url and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ModelServiceError("模型服务地址格式无效", configuration_error=True,
                                reason_category="service_configuration", field_path="base_url")
    path_key = "models_path" if protocol == "gemini" else "responses_path"
    if not isinstance(settings.get(path_key), str):
        raise ModelServiceError("模型请求路径未配置", configuration_error=True,
                                reason_category="service_configuration", field_path=path_key)
    timeout = settings.get("timeout_seconds")
    if type(timeout) is not int or not 5 <= timeout <= 600:
        raise ModelServiceError("模型请求超时配置无效", configuration_error=True,
                                reason_category="service_configuration", field_path="timeout_seconds")
    effort = settings.get("reasoning_effort", "")
    if not isinstance(effort, str):
        raise ModelServiceError("模型推理程度配置无效", configuration_error=True,
                                reason_category="service_configuration", field_path="reasoning_effort")
    if protocol == "gemini":
        return GeminiClient(GeminiConfig(
            base_url=settings["base_url"],
            models_path=settings["models_path"],
            api_key=api_key,
            timeout_seconds=settings["timeout_seconds"],
            reasoning_effort=effort,
        ))
    return ResponsesClient(ResponsesConfig(
        base_url=settings["base_url"],
        responses_path=settings["responses_path"],
        api_key=api_key,
        structured_output=settings.get("structured_output", True),
        timeout_seconds=settings["timeout_seconds"],
        reasoning_effort=effort,
    ))
