from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from .models import StructuredPageResult
from .prompts import PAGE_RESPONSE_SCHEMA


MAX_RESPONSE_BYTES = 2_000_000
MAX_OUTPUT_CHARS = 1_000_000
TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
UsageTuple = tuple[int | None, int | None, int | None]


class ModelServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResponsesConfig:
    base_url: str
    responses_path: str
    api_key: str
    structured_output: bool  # 兼容旧设置；页面请求固定使用 JSON Schema。
    timeout_seconds: int
    max_output_tokens: int
    reasoning_effort: str = ""

    @property
    def endpoint(self) -> str:
        base_url = self.base_url.rstrip("/")
        if not self.responses_path:
            return base_url
        return base_url + "/" + self.responses_path.lstrip("/")


class ResponsesClient:
    def __init__(self, config: ResponsesConfig):
        self.config = config

    async def request_page(
        self,
        model: str,
        input_value: list[dict[str, Any]],
        on_attempt_start: Callable[[], int] | None = None,
        on_attempt_end: Callable[[int, UsageTuple, bool], None] | None = None,
    ) -> StructuredPageResult:
        output_format: dict[str, Any] = {
            "type": "json_schema",
            "name": "page_transcription",
            "schema": PAGE_RESPONSE_SCHEMA,
        }
        if urlsplit(self.config.base_url).hostname != "api.deepseek.com":
            output_format["strict"] = True
        payload: dict[str, Any] = {
            "model": model,
            "input": input_value,
            "store": False,
            "max_output_tokens": self.config.max_output_tokens,
            "text": {"format": output_format},
        }
        if self.config.reasoning_effort:
            payload["reasoning"] = {"effort": self.config.reasoning_effort}
        data = await self._post(payload, on_attempt_start, on_attempt_end)
        response_text = self.extract_output_text(data)
        try:
            return StructuredPageResult.model_validate(json.loads(response_text))
        except (ValueError, ValidationError) as exc:
            raise ModelServiceError("模型返回的页面结构无效") from exc

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
            "max_output_tokens": self.config.max_output_tokens,
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
                raise ModelServiceError("模型已达输出上限（含推理），请增加输出额度或降低推理程度")
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
