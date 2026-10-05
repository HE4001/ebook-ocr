from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, TypeVar

import httpx
from pydantic import ValidationError

from .models import StructuredPageResult, Usage
from .prompts import PAGE_REPAIR_SCHEMA, PAGE_RESPONSE_VERSION, PAGE_REVIEW_SCHEMA, page_response_schema
from .workflow_model_contract import PageReview, RepairProposal


MAX_RESPONSE_BYTES = 2_000_000
MAX_OUTPUT_CHARS = 1_000_000
TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
UsageTuple = tuple[int | None, int | None, int | None]
AttemptStart = Callable[..., Any]
AttemptEnd = Callable[..., None]
ResultType = TypeVar("ResultType")


class ModelServiceError(RuntimeError):
    def __init__(
        self, message: str, *, configuration_error: bool = False,
        retryable: bool = False, result_uncertain: bool = False,
        status_code: int | None = None, usage: Usage | None = None,
        provider_request_id: str | None = None, received: bool = False,
    ):
        super().__init__(message)
        self.configuration_error = configuration_error
        self.retryable = retryable
        self.result_uncertain = result_uncertain
        self.status_code = status_code
        self.usage = usage
        self.provider_request_id = provider_request_id
        self.received = received


@dataclass(frozen=True)
class ProviderResponse:
    data: dict[str, Any]
    usage: Usage | None
    provider_request_id: str | None


def _usage_record(values: UsageTuple) -> Usage | None:
    if all(value is None for value in values):
        return None
    return Usage(
        input_tokens=values[0], output_tokens=values[1], total_tokens=values[2],
        complete=all(value is not None for value in values),
    )


def _provider_request_id(response: httpx.Response, data: dict[str, Any] | None) -> str | None:
    for name in ("x-request-id", "request-id", "x-goog-request-id"):
        value = response.headers.get(name)
        if value:
            return value[:200]
    if data is not None:
        for name in ("request_id", "responseId", "id"):
            value = data.get(name)
            if isinstance(value, str) and value:
                return value[:200]
    return None


def _endpoint_url(base_url: str, path: str) -> str:
    base_url = base_url.rstrip("/")
    return base_url + "/" + path.lstrip("/") if path else base_url


def _response_json(response: httpx.Response) -> dict[str, Any] | None:
    if len(response.content) > MAX_RESPONSE_BYTES:
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _http_error_message(status_code: int, data: dict[str, Any] | None, api_key: str) -> str:
    prefix = f"模型服务 HTTP {status_code}"
    error = data.get("error") if data is not None else None
    if not isinstance(error, dict):
        return prefix
    details: list[str] = []
    # 页面错误记录最多保存 1000 字符，给 param/code 预留空间。
    for name, limit in (("message", 600), ("param", 150), ("code", 150)):
        value = error.get(name)
        if not isinstance(value, str) or not value.strip():
            continue
        if api_key:
            value = value.replace(api_key, "[已隐藏]")
        value = " ".join(value.split())
        if len(value) > limit:
            value = value[:limit] + "…"
        details.append(value if name == "message" else f"{name}={value}")
    return f"{prefix}：{'；'.join(details)}" if details else prefix


async def _send_json(
    *, endpoint: str, headers: dict[str, str], payload: dict[str, Any],
    timeout_seconds: int, api_key: str, usage_reader: Callable[[dict[str, Any] | None], UsageTuple],
) -> ProviderResponse:
    """One physical request. Only the workflow scheduler may request a retry."""
    async with httpx.AsyncClient(
        follow_redirects=False, timeout=httpx.Timeout(timeout_seconds),
    ) as client:
        try:
            response = await client.post(endpoint, headers=headers, json=payload)
        except (httpx.InvalidURL, httpx.UnsupportedProtocol) as exc:
            raise ModelServiceError("模型服务地址或请求配置无效", configuration_error=True) from exc
        except httpx.LocalProtocolError as exc:
            raise ModelServiceError(
                "模型请求协议无效，发送结果不确定", configuration_error=True, result_uncertain=True,
            ) from exc
        except (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout) as exc:
            raise ModelServiceError("无法建立模型服务连接", retryable=True) from exc
        except httpx.TimeoutException as exc:
            raise ModelServiceError("模型服务请求超时，发送结果不确定", result_uncertain=True) from exc
        except httpx.RequestError as exc:
            raise ModelServiceError("模型服务连接中断，发送结果不确定", result_uncertain=True) from exc
    data = _response_json(response)
    details = {
        "status_code": response.status_code,
        "usage": _usage_record(usage_reader(data)),
        "provider_request_id": _provider_request_id(response, data),
        "received": True,
    }
    if 300 <= response.status_code < 400:
        raise ModelServiceError("模型服务返回重定向，已拒绝转发密钥", configuration_error=True, **details)
    if not response.is_success:
        raise ModelServiceError(
            _http_error_message(response.status_code, data, api_key),
            configuration_error=response.status_code in {400, 401, 403, 404, 405, 415, 422},
            retryable=response.status_code in TRANSIENT_STATUS, **details,
        )
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ModelServiceError("模型响应过大", **details)
    if data is None:
        raise ModelServiceError("模型服务响应格式无效", **details)
    return ProviderResponse(data, details["usage"], details["provider_request_id"])


async def _workflow_request(
    send: Callable[[], Awaitable[ProviderResponse]],
    parse: Callable[[dict[str, Any]], ResultType], *,
    on_attempt_start: AttemptStart, on_attempt_end: AttemptEnd, retry: bool,
    invalid_message: str,
) -> ResultType:
    reservation = on_attempt_start(retry=retry)
    attempt_id = getattr(reservation, "attempt_id", reservation)
    packet: ProviderResponse | None = None
    try:
        packet = await send()
        try:
            result = parse(packet.data)
        except (ValueError, ValidationError) as exc:
            raise ModelServiceError(invalid_message) from exc
    except ModelServiceError as exc:
        if packet is not None:
            exc.usage = packet.usage
            exc.provider_request_id = packet.provider_request_id
            exc.received = True
        on_attempt_end(
            attempt_id, state="unknown" if exc.result_uncertain else "failed",
            usage=exc.usage, provider_request_id=exc.provider_request_id, error=str(exc),
        )
        raise
    except asyncio.CancelledError:
        on_attempt_end(
            attempt_id, state="unknown", usage=packet.usage if packet is not None else None,
            provider_request_id=packet.provider_request_id if packet is not None else None,
            error="模型调用已中断，发送结果不确定；不自动重发",
        )
        raise
    on_attempt_end(
        attempt_id, state="succeeded", usage=packet.usage,
        provider_request_id=packet.provider_request_id, error=None,
    )
    return result


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
        raise ModelServiceError(_http_error_message(response.status_code, _response_json(response), api_key))
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

    @property
    def endpoint(self) -> str:
        return _endpoint_url(self.base_url, self.responses_path)


class ResponsesClient:
    def __init__(self, config: ResponsesConfig):
        self.config = config

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
            "store": False,
            "text": {"format": output_format},
        }
        if self.config.reasoning_effort:
            payload["reasoning"] = {"effort": self.config.reasoning_effort}
        return payload

    async def recognize_page(
        self, model: str, input_value: list[dict[str, Any]], *,
        on_attempt_start: AttemptStart, on_attempt_end: AttemptEnd, retry: bool = False,
    ) -> StructuredPageResult:
        payload = self._page_payload(
            model, input_value, "page_transcription_v2", page_response_schema(2),
        )
        return await _workflow_request(
            lambda: self._send(payload),
            lambda data: StructuredPageResult.from_model_response(json.loads(self.extract_output_text(data)), 2),
            on_attempt_start=on_attempt_start, on_attempt_end=on_attempt_end, retry=retry,
            invalid_message="模型返回的页面结构无效",
        )

    async def review_page(
        self, model: str, input_value: list[dict[str, Any]], *,
        on_attempt_start: AttemptStart, on_attempt_end: AttemptEnd, retry: bool = False,
    ) -> PageReview:
        payload = self._page_payload(model, input_value, "page_review_v1", PAGE_REVIEW_SCHEMA)
        return await _workflow_request(
            lambda: self._send(payload),
            lambda data: PageReview.model_validate(json.loads(self.extract_output_text(data))),
            on_attempt_start=on_attempt_start, on_attempt_end=on_attempt_end, retry=retry,
            invalid_message="模型返回的页面审查结构无效",
        )

    async def propose_repair(
        self, model: str, input_value: list[dict[str, Any]], *,
        on_attempt_start: AttemptStart, on_attempt_end: AttemptEnd, retry: bool = False,
    ) -> RepairProposal:
        payload = self._page_payload(model, input_value, "page_repair_v1", PAGE_REPAIR_SCHEMA)
        return await _workflow_request(
            lambda: self._send(payload),
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

    async def _send(self, payload: dict[str, Any]) -> ProviderResponse:
        return await _send_json(
            endpoint=self.config.endpoint,
            headers={"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"},
            payload=payload, timeout_seconds=self.config.timeout_seconds,
            api_key=self.config.api_key, usage_reader=self.response_usage,
        )

    async def _post(
        self, payload: dict[str, Any],
        on_attempt_start: Callable[[], int] | None = None,
        on_attempt_end: Callable[[int, UsageTuple, bool], None] | None = None,
    ) -> dict[str, Any]:
        # Keep old callbacks for explicit legacy tools; no hidden retry survives.
        attempt_id = on_attempt_start() if on_attempt_start else None
        try:
            packet = await self._send(payload)
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
