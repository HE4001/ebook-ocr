from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Literal
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from .models import StructuredPageResult
from .prompts import PAGE_REPAIR_SCHEMA, PAGE_RESPONSE_VERSION, PAGE_REVIEW_SCHEMA, page_response_schema
from .responses_client import (
    AttemptEnd,
    AttemptStart,
    MAX_OUTPUT_CHARS,
    MAX_RESPONSE_BYTES,
    ModelServiceError,
    ProviderResponse,
    UsageTuple,
    _endpoint_url,
    _http_error_message,
    _response_json,
    _send_json,
    _workflow_request,
)
from .workflow_model_contract import PageReview, RepairProposal


async def fetch_gemini_models(
    *, base_url: str, models_path: str, api_key: str, timeout_seconds: int,
) -> list[str]:
    model_ids: list[str] = []
    seen_tokens: set[str] = set()
    params: dict[str, str] = {}
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(timeout_seconds),
    ) as client:
        while True:
            try:
                response = await client.get(
                    _endpoint_url(base_url, models_path),
                    headers={"x-goog-api-key": api_key},
                    params=params,
                )
            except httpx.TimeoutException as exc:
                raise ModelServiceError("读取模型列表超时") from exc
            except httpx.RequestError as exc:
                raise ModelServiceError("无法连接模型服务") from exc
            if 300 <= response.status_code < 400:
                raise ModelServiceError("模型服务返回重定向，已拒绝转发密钥")
            if not response.is_success:
                raise ModelServiceError(_http_error_message(response.status_code, _response_json(response), api_key))
            if len(response.content) > MAX_RESPONSE_BYTES:
                raise ModelServiceError("模型列表响应过大")
            try:
                data = response.json()
            except ValueError as exc:
                raise ModelServiceError("模型列表响应格式无效") from exc
            if not isinstance(data, dict) or not isinstance(data.get("models", []), list):
                raise ModelServiceError("模型列表响应格式无效")
            for item in data.get("models", []):
                if not isinstance(item, dict):
                    raise ModelServiceError("模型列表响应格式无效")
                name = item.get("name")
                methods = item.get("supportedGenerationMethods", [])
                if (
                    not isinstance(name, str) or not name.strip()
                    or not isinstance(methods, list)
                    or any(not isinstance(method, str) for method in methods)
                ):
                    raise ModelServiceError("模型列表响应格式无效")
                if "generateContent" in methods:
                    model_ids.append(name)
            token = data.get("nextPageToken", "")
            if not isinstance(token, str):
                raise ModelServiceError("模型列表分页标记无效")
            if not token:
                return list(dict.fromkeys(model_ids))
            if token in seen_tokens:
                raise ModelServiceError("模型列表返回重复分页标记")
            seen_tokens.add(token)
            params = {"pageToken": token}


@dataclass(frozen=True)
class GeminiConfig:
    base_url: str
    models_path: str
    api_key: str
    timeout_seconds: int
    reasoning_effort: str = ""


class GeminiClient:
    def __init__(self, config: GeminiConfig):
        self.config = config

    def _generation_config(self) -> dict[str, Any]:
        effort = self.config.reasoning_effort.strip()
        if not effort:
            return {}
        if effort.lower() in {"minimal", "low", "medium", "high"}:
            thinking = {"thinkingLevel": effort.upper()}
        elif re.fullmatch(r"[+-]?[0-9]+", effort) and int(effort) >= -1:
            thinking = {"thinkingBudget": int(effort)}
        else:
            raise ModelServiceError(
                "Gemini 推理程度须为 minimal、low、medium、high 或不小于 -1 的整数",
                configuration_error=True,
            )
        return {"thinkingConfig": thinking}

    def _page_payload(
        self, input_value: list[dict[str, Any]], schema: dict[str, Any],
    ) -> dict[str, Any]:
        # Every workflow call contains a fresh system instruction and one user message.
        system_parts = [{"text": part["text"]} for part in input_value[0]["content"]]
        user_parts: list[dict[str, Any]] = []
        for part in input_value[1]["content"]:
            if part["type"] == "input_text":
                user_parts.append({"text": part["text"]})
            elif part["type"] == "input_image":
                user_parts.append({"inlineData": {
                    "mimeType": "image/png",
                    "data": part["image_url"].removeprefix("data:image/png;base64,"),
                }})
        user_content = {"role": "user", "parts": user_parts}
        generation_config = self._generation_config()
        generation_config.update({
            "responseMimeType": "application/json",
            "responseJsonSchema": schema,
        })
        return {
            "systemInstruction": {"parts": system_parts},
            "contents": [user_content],
            "generationConfig": generation_config,
        }

    async def recognize_page(
        self, model: str, input_value: list[dict[str, Any]], *,
        on_attempt_start: AttemptStart, on_attempt_end: AttemptEnd, retry: bool = False,
    ) -> StructuredPageResult:
        payload = self._page_payload(input_value, page_response_schema(2))
        return await _workflow_request(
            lambda: self._send(model, payload),
            lambda data: StructuredPageResult.from_model_response(json.loads(self.extract_output_text(data)), 2),
            on_attempt_start=on_attempt_start, on_attempt_end=on_attempt_end, retry=retry,
            invalid_message="模型返回的页面结构无效",
        )

    async def review_page(
        self, model: str, input_value: list[dict[str, Any]], *,
        on_attempt_start: AttemptStart, on_attempt_end: AttemptEnd, retry: bool = False,
    ) -> PageReview:
        payload = self._page_payload(input_value, PAGE_REVIEW_SCHEMA)
        return await _workflow_request(
            lambda: self._send(model, payload),
            lambda data: PageReview.model_validate(json.loads(self.extract_output_text(data))),
            on_attempt_start=on_attempt_start, on_attempt_end=on_attempt_end, retry=retry,
            invalid_message="模型返回的页面审查结构无效",
        )

    async def propose_repair(
        self, model: str, input_value: list[dict[str, Any]], *,
        on_attempt_start: AttemptStart, on_attempt_end: AttemptEnd, retry: bool = False,
    ) -> RepairProposal:
        payload = self._page_payload(input_value, PAGE_REPAIR_SCHEMA)
        return await _workflow_request(
            lambda: self._send(model, payload),
            lambda data: RepairProposal.model_validate(json.loads(self.extract_output_text(data))),
            on_attempt_start=on_attempt_start, on_attempt_end=on_attempt_end, retry=retry,
            invalid_message="模型返回的局部修复提案无效",
        )

    async def request_page(
        self,
        model: str,
        input_value: list[dict[str, Any]],
        on_attempt_start: Callable[[], int] | None = None,
        on_attempt_end: Callable[[int, UsageTuple, bool], None] | None = None,
        *, response_version: Literal[1, 2] = PAGE_RESPONSE_VERSION,
    ) -> StructuredPageResult:
        payload = self._page_payload(input_value, page_response_schema(response_version))
        data = await self._post(model, payload, on_attempt_start, on_attempt_end)
        response_text = self.extract_output_text(data)
        try:
            result = StructuredPageResult.from_model_response(json.loads(response_text), response_version)
        except (ValueError, ValidationError) as exc:
            raise ModelServiceError("模型返回的页面结构无效") from exc
        return result

    async def test_connection(self, model: str) -> None:
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": "只回复 OK。"}]}],
        }
        generation_config = self._generation_config()
        if generation_config:
            payload["generationConfig"] = generation_config
        data = await self._post(model, payload)
        self.extract_output_text(data)

    @staticmethod
    def response_usage(data: dict[str, Any] | None) -> UsageTuple:
        usage = data.get("usageMetadata") if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            return None, None, None

        def valid(key: str) -> int | None:
            value = usage.get(key)
            return value if type(value) is int and value >= 0 else None

        candidates = valid("candidatesTokenCount")
        thoughts = valid("thoughtsTokenCount") if "thoughtsTokenCount" in usage else 0
        output = candidates + thoughts if candidates is not None and thoughts is not None else None
        return valid("promptTokenCount"), output, valid("totalTokenCount")

    async def _send(self, model: str, payload: dict[str, Any]) -> ProviderResponse:
        model_id = model.strip().removeprefix("models/")
        if not model_id.strip():
            raise ModelServiceError("Gemini 模型名称不能为空", configuration_error=True)
        collection = _endpoint_url(self.config.base_url, self.config.models_path).rstrip("/")
        endpoint = f"{collection}/{quote(model_id, safe='')}:generateContent"
        return await _send_json(
            endpoint=endpoint,
            headers={"x-goog-api-key": self.config.api_key, "Content-Type": "application/json"},
            payload=payload, timeout_seconds=self.config.timeout_seconds,
            api_key=self.config.api_key, usage_reader=self.response_usage,
        )

    async def _post(
        self, model: str, payload: dict[str, Any],
        on_attempt_start: Callable[[], int] | None = None,
        on_attempt_end: Callable[[int, UsageTuple, bool], None] | None = None,
    ) -> dict[str, Any]:
        attempt_id = on_attempt_start() if on_attempt_start else None
        try:
            packet = await self._send(model, payload)
        except ModelServiceError as exc:
            if attempt_id is not None and on_attempt_end:
                usage = exc.usage
                values = (usage.input_tokens, usage.output_tokens, usage.total_tokens) if usage else (None, None, None)
                on_attempt_end(attempt_id, values, exc.received)
            raise
        except asyncio.CancelledError:
            if attempt_id is not None and on_attempt_end:
                on_attempt_end(attempt_id, (None, None, None), False)
            raise
        if attempt_id is not None and on_attempt_end:
            on_attempt_end(attempt_id, self.response_usage(packet.data), True)
        return packet.data

    @staticmethod
    def extract_output_text(data: dict[str, Any]) -> str:
        feedback = data.get("promptFeedback")
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            raise ModelServiceError("模型拒绝处理该页面")
        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
            raise ModelServiceError("模型响应没有可用候选内容")
        candidate = candidates[0]
        finish_reason = candidate.get("finishReason")
        if finish_reason == "MAX_TOKENS":
            raise ModelServiceError("模型输出已被服务端输出上限截断（含推理），可降低推理程度或更换模型")
        if finish_reason != "STOP":
            raise ModelServiceError("模型响应未成功完成或被内容过滤器截断")
        content = candidate.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            raise ModelServiceError("模型响应没有可用文本")
        texts = [
            part["text"] for part in parts
            if isinstance(part, dict) and not part.get("thought") and isinstance(part.get("text"), str)
        ]
        text = "".join(texts).strip()
        if not text:
            raise ModelServiceError("模型响应没有可用文本")
        if len(text) > MAX_OUTPUT_CHARS:
            raise ModelServiceError("模型输出文本过长")
        return text
