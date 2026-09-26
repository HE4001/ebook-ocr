from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from .models import PageKind, SpecialPageResult, StructuredPageResult
from .prompts import PAGE_RESPONSE_SCHEMA, SPECIAL_PAGE_RESPONSE_SCHEMA
from .responses_client import (
    MAX_OUTPUT_CHARS,
    MAX_RESPONSE_BYTES,
    TRANSIENT_STATUS,
    ModelServiceError,
    UsageTuple,
    _endpoint_url,
)


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
                raise ModelServiceError(f"模型服务 HTTP {response.status_code}")
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
    context_reuse_enabled: bool = False


class GeminiClient:
    def __init__(self, config: GeminiConfig):
        self.config = config
        self.contents: list[dict[str, Any]] = []

    def reset_context(self) -> None:
        self.contents.clear()

    def _generation_config(self) -> dict[str, Any]:
        effort = self.config.reasoning_effort.strip()
        if not effort:
            return {}
        if effort.lower() in {"minimal", "low", "medium", "high"}:
            thinking = {"thinkingLevel": effort.upper()}
        elif re.fullmatch(r"[+-]?[0-9]+", effort) and int(effort) >= -1:
            thinking = {"thinkingBudget": int(effort)}
        else:
            raise ModelServiceError("Gemini 推理程度须为 minimal、low、medium、high 或不小于 -1 的整数")
        return {"thinkingConfig": thinking}

    def _page_payload(
        self, input_value: list[dict[str, Any]], schema: dict[str, Any],
    ) -> dict[str, Any]:
        # pipeline 固定提供一条系统指令和一条含文本、PNG 的用户消息。
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

    async def request_page(
        self,
        model: str,
        input_value: list[dict[str, Any]],
        on_attempt_start: Callable[[], int] | None = None,
        on_attempt_end: Callable[[int, UsageTuple, bool], None] | None = None,
    ) -> StructuredPageResult:
        payload = self._page_payload(input_value, PAGE_RESPONSE_SCHEMA)
        user_content = payload["contents"][0]
        if self.config.context_reuse_enabled:
            payload["contents"] = [*self.contents, user_content]
        data = await self._post(model, payload, on_attempt_start, on_attempt_end)
        response_text = self.extract_output_text(data)
        try:
            parsed = json.loads(response_text)
            if not isinstance(parsed, dict) or not set(PAGE_RESPONSE_SCHEMA["required"]).issubset(parsed):
                raise ValueError("missing page fields")
            result = StructuredPageResult.model_validate(parsed)
        except (ValueError, ValidationError) as exc:
            raise ModelServiceError("模型返回的页面结构无效") from exc
        if result.cover_fields:
            raise ModelServiceError("普通页面代理识别到特殊页后应立即停止读取，不能返回书目信息")
        if self.config.context_reuse_enabled:
            # 保留完整模型内容，包括 thoughtSignature，供后续轮次原样回传。
            self.contents.extend([user_content, data["candidates"][0]["content"]])
        return result

    async def request_special_page(
        self,
        model: str,
        input_value: list[dict[str, Any]],
        page_kind: PageKind,
        on_attempt_start: Callable[[], int] | None = None,
        on_attempt_end: Callable[[int, UsageTuple, bool], None] | None = None,
    ) -> SpecialPageResult:
        payload = self._page_payload(input_value, SPECIAL_PAGE_RESPONSE_SCHEMA)
        data = await self._post(model, payload, on_attempt_start, on_attempt_end)
        response_text = self.extract_output_text(data)
        try:
            result = SpecialPageResult.model_validate(json.loads(response_text))
        except (ValueError, ValidationError) as exc:
            raise ModelServiceError("特殊页面代理返回的书目结构无效") from exc
        if result.page_kind != page_kind:
            raise ModelServiceError("特殊页面代理返回的页面类型与分流类型不一致")
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

    async def _post(
        self, model: str, payload: dict[str, Any],
        on_attempt_start: Callable[[], int] | None = None,
        on_attempt_end: Callable[[int, UsageTuple, bool], None] | None = None,
    ) -> dict[str, Any]:
        model_id = model.strip().removeprefix("models/")
        if not model_id.strip():
            raise ModelServiceError("Gemini 模型名称不能为空")
        collection = _endpoint_url(self.config.base_url, self.config.models_path).rstrip("/")
        endpoint = f"{collection}/{quote(model_id, safe='')}:generateContent"
        headers = {
            "x-goog-api-key": self.config.api_key,
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
                    response = await client.post(endpoint, headers=headers, json=payload)
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
                    elif data is None:
                        raise ModelServiceError("模型服务响应格式无效")
                    else:
                        return data
                if not retry or attempt == 2:
                    raise ModelServiceError(last_message)
                await asyncio.sleep(2**attempt)
        raise ModelServiceError(last_message)

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
