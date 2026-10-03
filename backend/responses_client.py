from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Callable, Literal

import httpx
from pydantic import ValidationError

from .models import StructuredPageResult
from .prompts import PAGE_RESPONSE_VERSION, page_response_schema


MAX_RESPONSE_BYTES = 2_000_000
MAX_OUTPUT_CHARS = 1_000_000
TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
UsageTuple = tuple[int | None, int | None, int | None]


class ModelServiceError(RuntimeError):
    pass


def _endpoint_url(base_url: str, path: str) -> str:
    base_url = base_url.rstrip("/")
    return base_url + "/" + path.lstrip("/") if path else base_url


async def fetch_models(
    *, base_url: str, models_path: str, api_key: str, timeout_seconds: int,
) -> list[str]:
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(timeout_seconds),
    ) as client:
        try:
            response = await client.get(
                _endpoint_url(base_url, models_path),
                headers={"Authorization": f"Bearer {api_key}"},
            )
        except httpx.TimeoutException as exc:
            raise ModelServiceError("读取模型列表超时") from exc
        except httpx.RequestError as exc:
            raise ModelServiceError("无法连接模型服务") from exc
    if 300 <= response.status_code < 400:
        raise ModelServiceError("模型服务返回重定向，已拒绝转发密钥")
    if not response.is_success:
        raise ModelServiceError(f"模型服务 HTTP {response.status_code}")
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ModelServiceError("模型列表响应过大")
    try:
        data = response.json()
    except ValueError as exc:
        raise ModelServiceError("模型列表响应格式无效") from exc
    if not isinstance(data, dict) or data.get("object") != "list" or not isinstance(data.get("data"), list):
        raise ModelServiceError("模型列表响应格式无效")
    model_ids: list[str] = []
    for item in data["data"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
            raise ModelServiceError("模型列表响应格式无效")
        model_ids.append(item["id"])
    return list(dict.fromkeys(model_ids))


@dataclass(frozen=True)
class ResponsesConfig:
    base_url: str
    responses_path: str
    api_key: str
    structured_output: bool  # 兼容旧设置；页面请求固定使用 JSON Schema。
    timeout_seconds: int
    reasoning_effort: str = ""
    context_reuse_enabled: bool = False

    @property
    def endpoint(self) -> str:
        return _endpoint_url(self.base_url, self.responses_path)


class ResponsesClient:
    def __init__(self, config: ResponsesConfig):
        self.config = config
        self.previous_response_id: str | None = None

    def reset_context(self) -> None:
        self.previous_response_id = None

    def _page_payload(
        self, model: str, input_value: list[dict[str, Any]],
        schema_name: str, schema: dict[str, Any],
    ) -> dict[str, Any]:
        output_format: dict[str, Any] = {
            "type": "json_schema",
            "name": schema_name,
            "schema": schema,
            "strict": True,
        }
        payload: dict[str, Any] = {
            "model": model,
            "input": input_value,
            "store": self.config.context_reuse_enabled,
            "text": {"format": output_format},
        }
        if self.config.context_reuse_enabled and self.previous_response_id is not None:
            payload["previous_response_id"] = self.previous_response_id
        if self.config.reasoning_effort:
            payload["reasoning"] = {"effort": self.config.reasoning_effort}
        return payload

    async def request_page(
        self,
        model: str,
        input_value: list[dict[str, Any]],
        on_attempt_start: Callable[[], int] | None = None,
        on_attempt_end: Callable[[int, UsageTuple, bool], None] | None = None,
        *, response_version: Literal[1, 2] = PAGE_RESPONSE_VERSION,
    ) -> StructuredPageResult:
        payload = self._page_payload(
            model, input_value, f"page_transcription_v{response_version}",
            page_response_schema(response_version),
        )
        data = await self._post(payload, on_attempt_start, on_attempt_end)
        response_text = self.extract_output_text(data)
        try:
            result = StructuredPageResult.from_model_response(json.loads(response_text), response_version)
        except (ValueError, ValidationError) as exc:
            raise ModelServiceError("模型返回的页面结构无效") from exc
        if self.config.context_reuse_enabled:
            response_id = data.get("id")
            if not isinstance(response_id, str) or not response_id.strip():
                raise ModelServiceError("模型接口未返回有效 response id，不支持实验性上下文续接")
            self.previous_response_id = response_id
        return result

    async def test_connection(self, model: str) -> None:
        payload = {
            "model": model,
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "只回复 OK。"}],
                }
            ],
            "store": False,
        }
        if self.config.reasoning_effort:
            payload["reasoning"] = {"effort": self.config.reasoning_effort}
        data = await self._post(payload)
        self.extract_output_text(data)

    @staticmethod
    def response_usage(data: dict[str, Any] | None) -> UsageTuple:
        usage = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            return None, None, None
        def valid(key: str) -> int | None:
            value = usage.get(key)
            return value if type(value) is int and value >= 0 else None
        return valid("input_tokens"), valid("output_tokens"), valid("total_tokens")

    async def _post(
        self, payload: dict[str, Any],
        on_attempt_start: Callable[[], int] | None = None,
        on_attempt_end: Callable[[int, UsageTuple, bool], None] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        last_message = "模型服务请求失败"
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(self.config.timeout_seconds),
        ) as client:
            for attempt in range(3):
                attempt_id = on_attempt_start() if on_attempt_start else None
                try:
                    response = await client.post(self.config.endpoint, headers=headers, json=payload)
                except httpx.TimeoutException:
                    if attempt_id is not None and on_attempt_end:
                        on_attempt_end(attempt_id, (None, None, None), False)
                    last_message = "模型服务请求超时"
                    retry = True
                except httpx.RequestError:
                    if attempt_id is not None and on_attempt_end:
                        on_attempt_end(attempt_id, (None, None, None), False)
                    last_message = "无法连接模型服务"
                    retry = True
                else:
                    data: dict[str, Any] | None = None
                    if len(response.content) <= MAX_RESPONSE_BYTES:
                        try:
                            parsed = response.json()
                            data = parsed if isinstance(parsed, dict) else None
                        except ValueError:
                            pass
                    if attempt_id is not None and on_attempt_end:
                        on_attempt_end(attempt_id, self.response_usage(data), True)
                    retry = response.status_code in TRANSIENT_STATUS
                    if 300 <= response.status_code < 400:
                        raise ModelServiceError("模型服务返回重定向，已拒绝转发密钥")
                    if not response.is_success:
                        last_message = f"模型服务 HTTP {response.status_code}"
                        if not retry:
                            raise ModelServiceError(last_message)
                    elif len(response.content) > MAX_RESPONSE_BYTES:
                        raise ModelServiceError("模型响应过大")
                    else:
                        if data is None:
                            raise ModelServiceError("模型服务响应格式无效")
                        return data
                if not retry or attempt == 2:
                    raise ModelServiceError(last_message)
                await asyncio.sleep(2**attempt)
        raise ModelServiceError(last_message)

    @staticmethod
    def extract_output_text(data: dict[str, Any], *, allow_empty: bool = False) -> str:
        status = data.get("status")
        if status == "incomplete":
            details = data.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, dict) else None
            if reason == "max_output_tokens":
                raise ModelServiceError("模型已达服务端输出上限（含推理），可降低推理程度或更换模型")
            if reason == "content_filter":
                raise ModelServiceError("模型输出被内容过滤器截断")
            raise ModelServiceError("模型响应未完成")
        if isinstance(status, str) and status != "completed":
            raise ModelServiceError("模型响应未成功完成")
        texts: list[str] = []
        has_text_part = False
        output = data.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict) or item.get("type") != "message":
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "refusal" or part.get("refusal"):
                        raise ModelServiceError("模型拒绝处理该页面")
                    if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                        has_text_part = True
                        texts.append(part["text"])
        top_level = data.get("output_text")
        if not has_text_part and isinstance(top_level, str):
            has_text_part = True
            texts.append(top_level)
        text = "".join(texts).strip()
        if not text and (not allow_empty or not has_text_part):
            raise ModelServiceError("模型响应没有可用文本")
        if len(text) > MAX_OUTPUT_CHARS:
            raise ModelServiceError("模型输出文本过长")
        return text
