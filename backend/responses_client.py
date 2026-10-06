from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Generic, Literal, TypeVar

import httpx
from pydantic import ValidationError

from .content_contract import BlockParseFailure, ContentParseResult, PageContent, parse_content_response
from .layout_contract import BBox, CoarseRegion, CropMapping, RecognitionInput
from .models import RecognitionResponse, StructuredPageResult, Usage
from .prompts import (
    CONTENT_ORDER_PROMPT, CONTENT_ORDER_SCHEMA, CONTENT_RECOGNITION_PROMPT,
    CONTENT_REREAD_PROMPT, CONTENT_REVIEW_PROMPT, CONTENT_REVIEW_SCHEMA, ContentKind,
    PAGE_REPAIR_SCHEMA, PAGE_RESPONSE_VERSION, PAGE_REVIEW_SCHEMA, content_candidate,
    content_request_input, content_response_schema, page_response_schema,
)
from .workflow_model_contract import ContentReview, PageReview, ReadingOrder, RepairProposal


MAX_RESPONSE_BYTES = 2_000_000
MAX_OUTPUT_CHARS = 1_000_000
TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
CONFIGURATION_STATUS = {400, 401, 402, 403, 404, 405, 415, 422}
UsageTuple = tuple[int | None, int | None, int | None]
AttemptStart = Callable[..., Any]
AttemptEnd = Callable[..., None]
ResultType = TypeVar("ResultType")
PersistResponse = Callable[..., RecognitionResponse]
CompletionStatus = Literal["complete", "truncated", "incomplete", "unknown"]


class ModelServiceError(RuntimeError):
    def __init__(
        self, message: str, *, configuration_error: bool = False,
        retryable: bool = False, result_uncertain: bool = False,
        status_code: int | None = None, usage: Usage | None = None,
        provider_request_id: str | None = None, received: bool = False,
        stage: str | None = None, reason_category: str | None = None,
        field_path: str | None = None, completion_status: CompletionStatus = "unknown",
        response_body: str | None = None, response_id: str | None = None,
        received_not_saved: bool = False,
    ):
        super().__init__(message)
        self.configuration_error = configuration_error
        self.retryable = retryable
        self.result_uncertain = result_uncertain
        self.status_code = status_code
        self.usage = usage
        self.provider_request_id = provider_request_id
        self.received = received
        self.stage = stage
        self.reason_category = reason_category
        self.field_path = field_path
        self.completion_status = completion_status
        self.response_body = response_body  # local persistence only; never include in str(exc)
        self.response_id = response_id
        self.received_not_saved = received_not_saved


@dataclass(frozen=True)
class ProviderResponse:
    data: dict[str, Any]
    usage: Usage | None
    provider_request_id: str | None
    raw_body: str = ""


@dataclass(frozen=True)
class ProviderText:
    body: str
    completion_status: CompletionStatus
    reason_category: str | None = None
    reason: str | None = None
    field_path: str | None = None


@dataclass(frozen=True)
class RequestDiagnostic:
    stage: str
    category: str
    field_path: str
    reason: str


class ResponseContractError(ValueError):
    def __init__(self, reason: str, field_path: str):
        super().__init__(reason)
        self.field_path = field_path


@dataclass(frozen=True)
class ModelRequestResult(Generic[ResultType]):
    attempt_id: str
    response_id: str
    completion_status: CompletionStatus
    usage: Usage | None
    provider_request_id: str | None
    parsed: ResultType | None
    diagnostics: list[RequestDiagnostic] = field(default_factory=list)


ContentRequestResult = ModelRequestResult[ContentParseResult]
ReviewRequestResult = ModelRequestResult[ContentReview]
OrderRequestResult = ModelRequestResult[ReadingOrder]


def _bounded_body(body: str, api_key: str) -> tuple[str, bool]:
    if api_key:
        body = body.replace(api_key, "[已隐藏]")
    body = re.sub(r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+", "[图像数据已省略]", body)
    body = re.sub(r'("(?:authorization|x-goog-api-key|api_key)"\s*:\s*")[^"\r\n]*',
                  r'\1[已隐藏]', body, flags=re.IGNORECASE)
    body = re.sub(r'("inlineData"\s*:\s*\{[^{}]*"data"\s*:\s*")[A-Za-z0-9+/=]+',
                  r'\1[图像数据已省略]', body)
    data = body.encode("utf-8")
    truncated = len(body) > MAX_OUTPUT_CHARS or len(data) > MAX_RESPONSE_BYTES
    if truncated:
        body = data[:MAX_RESPONSE_BYTES].decode("utf-8", errors="ignore")[:MAX_OUTPUT_CHARS]
    return body, truncated


def _usage_record(values: UsageTuple) -> Usage | None:
    if all(value is None for value in values):
        return None
    return Usage(
        input_tokens=values[0], output_tokens=values[1], total_tokens=values[2],
        complete=all(value is not None for value in values),
    )


def _provider_request_id(response: httpx.Response, data: dict[str, Any] | None, api_key: str = "") -> str | None:
    for name in ("x-request-id", "request-id", "x-goog-request-id"):
        value = response.headers.get(name)
        if value:
            return (value.replace(api_key, "[已隐藏]") if api_key else value)[:200]
    if data is not None:
        for name in ("request_id", "responseId", "id"):
            value = data.get(name)
            if isinstance(value, str) and value:
                return (value.replace(api_key, "[已隐藏]") if api_key else value)[:200]
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


def _saved_http_status(error: str | None) -> int | None:
    """Read only the status prefix emitted by the shared HTTP client."""
    match = re.match(r"模型服务 HTTP (\d{3})(?:[：；]|$)", error or "")
    return int(match[1]) if match else None


async def _send_json(
    *, endpoint: str, headers: dict[str, str], payload: dict[str, Any],
    timeout_seconds: int, api_key: str, usage_reader: Callable[[dict[str, Any] | None], UsageTuple],
) -> ProviderResponse:
    """One physical request. Only the workflow scheduler may request a retry."""
    async with httpx.AsyncClient(
        follow_redirects=False, timeout=httpx.Timeout(timeout_seconds),
    ) as client:
        response: httpx.Response | None = None
        chunks: list[bytes] = []
        try:
            async with client.stream("POST", endpoint, headers=headers, json=payload) as response:
                size = 0
                oversized = False
                async for chunk in response.aiter_bytes():
                    remaining = MAX_RESPONSE_BYTES - size
                    chunks.append(chunk[:remaining])
                    size += len(chunk[:remaining])
                    if len(chunk) > remaining:
                        oversized = True
                        break
                raw = b"".join(chunks)
        except (httpx.InvalidURL, httpx.UnsupportedProtocol) as exc:
            raise ModelServiceError("模型服务地址或请求配置无效", configuration_error=True) from exc
        except httpx.LocalProtocolError as exc:
            raise ModelServiceError(
                "模型请求协议无效，发送结果不确定", configuration_error=True, result_uncertain=True,
            ) from exc
        except (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout) as exc:
            raise ModelServiceError("无法建立模型服务连接", retryable=True) from exc
        except httpx.TimeoutException as exc:
            body, cut = _bounded_body(b"".join(chunks).decode("utf-8", errors="replace"), api_key)
            raise ModelServiceError(
                "模型服务请求超时，发送结果不确定", result_uncertain=True,
                received=response is not None, response_body=body if response is not None else None,
                completion_status="truncated" if cut else "incomplete",
                provider_request_id=_provider_request_id(response, None, api_key) if response is not None else None,
            ) from exc
        except httpx.RequestError as exc:
            body, cut = _bounded_body(b"".join(chunks).decode("utf-8", errors="replace"), api_key)
            raise ModelServiceError(
                "模型服务连接中断，发送结果不确定", result_uncertain=True,
                received=response is not None, response_body=body if response is not None else None,
                completion_status="truncated" if cut else "incomplete",
                provider_request_id=_provider_request_id(response, None, api_key) if response is not None else None,
            ) from exc
    raw_body, body_cut = _bounded_body(raw.decode("utf-8", errors="replace"), api_key)
    try:
        value = json.loads(raw) if not oversized else None
    except (ValueError, RecursionError):
        value = None
    data = value if isinstance(value, dict) else None
    details = {
        "status_code": response.status_code,
        "usage": _usage_record(usage_reader(data)),
        "provider_request_id": _provider_request_id(response, data, api_key),
        "received": True,
        "response_body": raw_body,
        "completion_status": "truncated" if oversized or body_cut else "unknown",
    }
    if 300 <= response.status_code < 400:
        raise ModelServiceError("模型服务返回重定向，已拒绝转发密钥", configuration_error=True, **details)
    if not response.is_success:
        raise ModelServiceError(
            _http_error_message(response.status_code, data, api_key),
            configuration_error=response.status_code in CONFIGURATION_STATUS,
            reason_category="service_configuration" if response.status_code in CONFIGURATION_STATUS else None,
            retryable=response.status_code in TRANSIENT_STATUS, **details,
        )
    if oversized or body_cut:
        raise ModelServiceError("模型响应超过保存上限，已保留有界正文", reason_category="truncated_response", **details)
    if data is None:
        raise ModelServiceError("模型服务响应信封格式无效", reason_category="invalid_structure", field_path="$", **details)
    return ProviderResponse(data, details["usage"], details["provider_request_id"], raw_body)


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


def _save_v2_body(
    persist_response: PersistResponse, attempt_id: str, body: str, *,
    completion_status: CompletionStatus, usage: Usage | None, provider_request_id: str | None,
    stage: str,
) -> RecognitionResponse:
    try:
        record = persist_response(
            attempt_id, body, completion_status=completion_status,
            usage=usage, provider_request_id=provider_request_id,
        )
        if record.body_storage != "saved":
            raise OSError("响应正文未保存")
        response_id = record.attempt_id
        if response_id != attempt_id:
            raise ValueError("响应保存未返回身份")
        return record
    except Exception as exc:
        raise ModelServiceError(
            "已收到模型响应，但正文未可靠保存；不会自动重发", received=True,
            received_not_saved=True, result_uncertain=True, stage=stage,
            reason_category="unknown_consumption", field_path="response.body_storage",
            completion_status=completion_status, usage=usage, provider_request_id=provider_request_id,
            response_id=attempt_id,
        ) from exc


def _parse_diagnostic(stage: str, error: ValueError | RecursionError) -> RequestDiagnostic:
    if isinstance(error, ValidationError):
        detail = error.errors(include_input=False, include_context=False)[0]
        path = ".".join(str(part) for part in detail["loc"]) or "$"
        reason = detail["msg"][:500]
    elif isinstance(error, json.JSONDecodeError):
        path = f"$@{error.pos}"
        reason = "响应正文不是完整合法 JSON"
    elif isinstance(error, ResponseContractError):
        path, reason = error.field_path, str(error)[:500]
    else:
        path = "$"
        reason = "响应字段与本次职责或目标修订不一致"
    return RequestDiagnostic(stage, "invalid_structure", path[:300], reason)


async def _v2_request(
    send: Callable[[], Awaitable[ProviderResponse]],
    extract: Callable[[ProviderResponse], ProviderText], parse: Callable[[str, str], ResultType], *,
    stage: str, api_key: str, on_attempt_start: AttemptStart, on_attempt_end: AttemptEnd,
    persist_response: PersistResponse, retry: bool,
) -> ModelRequestResult[ResultType]:
    """Reserve once, send once, persist before strict content parsing."""
    reservation = on_attempt_start(retry=retry)
    attempt_id = getattr(reservation, "attempt_id", reservation)
    packet: ProviderResponse | None = None
    response_id: str | None = None
    try:
        packet = await send()
        text = extract(packet)
        body, cut = _bounded_body(text.body, api_key)
        completion = "truncated" if cut else text.completion_status
        record = _save_v2_body(
            persist_response, attempt_id, body, completion_status=completion,
            usage=packet.usage, provider_request_id=packet.provider_request_id, stage=stage,
        )
        response_id = record.attempt_id
        completion = "truncated" if record.body_truncated else record.completion_status
        diagnostics: list[RequestDiagnostic] = []
        parsed: ResultType | None = None
        if completion != "complete":
            diagnostics.append(RequestDiagnostic(
                stage, "truncated_response" if completion == "truncated" else (text.reason_category or "invalid_structure"),
                text.field_path or "$", text.reason or "模型响应未完成；已保存正文，不能作为完整内容采用",
            ))
        else:
            try:
                parsed = parse(body, response_id)
            except (ValueError, RecursionError) as exc:
                diagnostics.append(_parse_diagnostic(stage, exc))
            if isinstance(parsed, ContentParseResult):
                diagnostics.extend(RequestDiagnostic(stage, failure.category, failure.field_path, failure.reason)
                                   for failure in parsed.failures)
                if parsed.content is not None:
                    diagnostics.extend(RequestDiagnostic(stage, issue.category, issue.field_path or "$", issue.reason)
                                       for issue in parsed.content.issues if issue.response_index is None)
        result = ModelRequestResult(
            attempt_id=attempt_id, response_id=response_id, completion_status=completion,
            usage=packet.usage, provider_request_id=packet.provider_request_id,
            parsed=parsed, diagnostics=diagnostics,
        )
    except ModelServiceError as exc:
        exc.stage = exc.stage or stage
        if exc.reason_category is None:
            exc.reason_category = ("service_configuration" if exc.configuration_error else
                                   "unknown_consumption" if exc.result_uncertain else
                                   "temporary_service" if exc.retryable else "invalid_structure")
        if packet is not None:
            exc.usage, exc.provider_request_id, exc.received = packet.usage, packet.provider_request_id, True
        if exc.received and exc.response_body is not None and not exc.received_not_saved:
            body, cut = _bounded_body(exc.response_body, api_key)
            try:
                record = _save_v2_body(
                    persist_response, attempt_id, body,
                    completion_status="truncated" if cut else exc.completion_status,
                    usage=exc.usage, provider_request_id=exc.provider_request_id, stage=stage,
                )
                exc.response_id = record.attempt_id
                exc.completion_status = "truncated" if record.body_truncated else record.completion_status
            except ModelServiceError as save_error:
                if exc.configuration_error:
                    # A body write failure cannot make a known service refusal
                    # eligible for subsequent model calls.
                    save_error.configuration_error = True
                    save_error.reason_category = "service_configuration"
                    save_error.status_code = exc.status_code
                    save_error.args = (f"{exc}；{save_error}",)
                on_attempt_end(attempt_id, state="unknown", usage=save_error.usage,
                               provider_request_id=save_error.provider_request_id, error=str(save_error))
                raise save_error from exc
            finally:
                exc.response_body = None
        on_attempt_end(
            attempt_id, state="unknown" if exc.result_uncertain else "failed", usage=exc.usage,
            provider_request_id=exc.provider_request_id, error=str(exc),
            retryable=exc.retryable and not exc.configuration_error and not exc.result_uncertain,
        )
        raise
    except asyncio.CancelledError:
        on_attempt_end(
            attempt_id, state="unknown", usage=packet.usage if packet else None,
            provider_request_id=packet.provider_request_id if packet else None,
            error="模型调用已中断；不自动重发已发送或消费未知的请求",
        )
        raise
    on_attempt_end(
        attempt_id, state="succeeded" if completion == "complete" and parsed is not None else "failed",
        usage=packet.usage, provider_request_id=packet.provider_request_id,
        error=diagnostics[0].reason[:500] if diagnostics else None,
    )
    return result


class ContentClientV2:
    """Shared V2 responsibilities; each provider supplies one payload and send."""

    def _v2_payload(self, model: str, input_value: list[dict[str, Any]],
                    name: str, schema: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def _v2_send(self, model: str, payload: dict[str, Any]) -> ProviderResponse:
        raise NotImplementedError

    def _v2_text(self, packet: ProviderResponse) -> ProviderText:
        raise NotImplementedError

    async def _content_call(
        self, model: str, inputs: list[RecognitionInput], *, book_id: str, page_id: str,
        source_version: int, on_attempt_start: AttemptStart, on_attempt_end: AttemptEnd,
        persist_response: PersistResponse, content_kind: ContentKind | None,
        coarse_regions: list[CoarseRegion] | tuple[CoarseRegion, ...], native_text_evidence: str | None,
        target_bbox: BBox | None, target_crop: CropMapping | None, source_region_id: str | None,
        retry: bool, stage: str,
    ) -> ContentRequestResult:
        if any(item.mapping is not None and (item.mapping.page_id, item.mapping.source_version) !=
               (page_id, source_version) for item in inputs):
            raise ModelServiceError("源页裁切映射版本不匹配", configuration_error=True,
                                    stage=stage, field_path="inputs.mapping")
        prompt = CONTENT_REREAD_PROMPT if stage == "recover" else CONTENT_RECOGNITION_PROMPT
        try:
            input_value = await asyncio.to_thread(
                content_request_input, inputs, prompt=prompt, content_kind=content_kind,
                coarse_regions=coarse_regions, native_text_evidence=native_text_evidence,
                target_bbox=target_bbox, target_crop=target_crop,
            )
        except OSError as exc:
            raise ModelServiceError("本次源页模型输入不可用", stage=stage,
                                    reason_category="unreadable_source", field_path="inputs") from exc
        except ValueError as exc:
            raise ModelServiceError(str(exc)[:500], stage=stage,
                                    reason_category="invalid_structure", field_path="inputs") from exc
        try:
            payload = self._v2_payload(model, input_value, "ocr_content_v2", content_response_schema(content_kind))
        except ModelServiceError as exc:
            exc.stage = exc.stage or stage
            exc.reason_category = exc.reason_category or "service_configuration"
            raise

        def parse(body: str, response_id: str) -> ContentParseResult:
            result = parse_content_response(body, book_id=book_id, page_id=page_id,
                                            source_version=source_version, response_id=response_id,
                                            crop_mapping=target_crop)
            if result.content is not None and source_region_id is not None:
                for block in result.content.blocks:
                    block.source_region_id = source_region_id
            return result

        return await _v2_request(
            lambda: self._v2_send(model, payload), self._v2_text, parse,
            stage=stage, api_key=self.config.api_key, on_attempt_start=on_attempt_start,
            on_attempt_end=on_attempt_end, persist_response=persist_response, retry=retry,
        )

    async def recognize_content(
        self, model: str, inputs: list[RecognitionInput], *, book_id: str, page_id: str,
        source_version: int, on_attempt_start: AttemptStart, on_attempt_end: AttemptEnd,
        persist_response: PersistResponse, content_kind: ContentKind | None = None,
        coarse_regions: list[CoarseRegion] | tuple[CoarseRegion, ...] = (),
        native_text_evidence: str | None = None, retry: bool = False,
    ) -> ContentRequestResult:
        return await self._content_call(
            model, inputs, book_id=book_id, page_id=page_id, source_version=source_version,
            on_attempt_start=on_attempt_start, on_attempt_end=on_attempt_end,
            persist_response=persist_response, content_kind=content_kind, coarse_regions=coarse_regions,
            native_text_evidence=native_text_evidence, target_bbox=None, target_crop=None,
            source_region_id=None, retry=retry, stage="recognize",
        )

    async def reread_content(
        self, model: str, inputs: list[RecognitionInput], *, book_id: str, page_id: str,
        source_version: int, target_bbox: BBox, target_kind: ContentKind,
        on_attempt_start: AttemptStart, on_attempt_end: AttemptEnd, persist_response: PersistResponse,
        target_crop: CropMapping | None = None, source_region_id: str | None = None, retry: bool = False,
    ) -> ContentRequestResult:
        # No old candidate text or prior response is accepted by this interface.
        return await self._content_call(
            model, inputs, book_id=book_id, page_id=page_id, source_version=source_version,
            on_attempt_start=on_attempt_start, on_attempt_end=on_attempt_end,
            persist_response=persist_response, content_kind=target_kind, coarse_regions=(),
            native_text_evidence=None, target_bbox=target_bbox, target_crop=target_crop,
            source_region_id=source_region_id, retry=retry, stage="recover",
        )

    async def review_content(
        self, model: str, inputs: list[RecognitionInput], content: PageContent, *,
        on_attempt_start: AttemptStart, on_attempt_end: AttemptEnd, persist_response: PersistResponse,
        retry: bool = False,
    ) -> ReviewRequestResult:
        return await self._review_or_order(model, inputs, content, on_attempt_start=on_attempt_start,
                                         on_attempt_end=on_attempt_end, persist_response=persist_response,
                                         retry=retry, order_only=False)

    async def reading_order(
        self, model: str, inputs: list[RecognitionInput], content: PageContent, *,
        on_attempt_start: AttemptStart, on_attempt_end: AttemptEnd, persist_response: PersistResponse,
        retry: bool = False,
    ) -> OrderRequestResult:
        return await self._review_or_order(model, inputs, content, on_attempt_start=on_attempt_start,
                                         on_attempt_end=on_attempt_end, persist_response=persist_response,
                                         retry=retry, order_only=True)

    async def _review_or_order(
        self, model: str, inputs: list[RecognitionInput], content: PageContent, *,
        on_attempt_start: AttemptStart, on_attempt_end: AttemptEnd, persist_response: PersistResponse,
        retry: bool, order_only: bool,
    ) -> ReviewRequestResult | OrderRequestResult:
        stage = "reading_order" if order_only else "review"
        if any(item.mapping is not None and (item.mapping.page_id, item.mapping.source_version) !=
               (content.page_id, content.source_version) for item in inputs):
            raise ModelServiceError("复核裁切与内容来源版本不匹配", configuration_error=True,
                                    stage=stage, field_path="inputs.mapping")
        try:
            input_value = await asyncio.to_thread(
                content_request_input, inputs,
                prompt=CONTENT_ORDER_PROMPT if order_only else CONTENT_REVIEW_PROMPT,
                candidate=content_candidate(content, order_only=order_only),
            )
        except OSError as exc:
            raise ModelServiceError("完整源页复核输入不可用", stage=stage,
                                    reason_category="unreadable_source", field_path="inputs") from exc
        except ValueError as exc:
            raise ModelServiceError(str(exc)[:500], stage=stage,
                                    reason_category="invalid_structure", field_path="inputs") from exc
        schema = CONTENT_ORDER_SCHEMA if order_only else CONTENT_REVIEW_SCHEMA
        try:
            payload = self._v2_payload(model, input_value, "ocr_order_v2" if order_only else "ocr_review_v2", schema)
        except ModelServiceError as exc:
            exc.stage = exc.stage or stage
            exc.reason_category = exc.reason_category or "service_configuration"
            raise

        def parse(body: str, response_id: str) -> ContentReview | ReadingOrder:
            if order_only:
                return self.parse_saved_order(body, content, completion_status="complete")
            return self.parse_saved_review(body, content, completion_status="complete")

        return await _v2_request(
            lambda: self._v2_send(model, payload), self._v2_text, parse,
            stage=stage, api_key=self.config.api_key,
            on_attempt_start=on_attempt_start, on_attempt_end=on_attempt_end,
            persist_response=persist_response, retry=retry,
        )

    @staticmethod
    def parse_saved_content(
        body: str, *, book_id: str, page_id: str, source_version: int, response_id: str,
        completion_status: CompletionStatus,
        content_revision_id: str | None = None, crop_mapping: CropMapping | None = None,
    ) -> ContentParseResult:
        if completion_status != "complete":
            return ContentParseResult(failures=[BlockParseFailure(
                field_path="$", reason="已保存响应未完整完成，不能作为完整内容解析",
                category="truncated_response" if completion_status == "truncated" else "invalid_structure",
            )])
        return parse_content_response(body, book_id=book_id, page_id=page_id,
                                      source_version=source_version, response_id=response_id,
                                      content_revision_id=content_revision_id, crop_mapping=crop_mapping)

    @staticmethod
    def parse_saved_review(body: str, content: PageContent, *,
                           completion_status: CompletionStatus) -> ContentReview:
        if completion_status != "complete":
            raise ResponseContractError("已保存复核响应未完整完成", "completion_status")
        review = ContentReview.model_validate(json.loads(body))
        if review.base_content_revision_id != content.content_revision_id:
            raise ResponseContractError("复核目标修订不匹配", "base_content_revision_id")
        block_ids = {block.block_id for block in content.blocks}
        for index, issue in enumerate(review.issues):
            if issue.block_id is not None and issue.block_id not in block_ids:
                raise ResponseContractError("复核问题引用了不存在的候选块", f"issues[{index}].block_id")
        return review

    @staticmethod
    def parse_saved_order(body: str, content: PageContent, *,
                          completion_status: CompletionStatus) -> ReadingOrder:
        if completion_status != "complete":
            raise ResponseContractError("已保存阅读顺序响应未完整完成", "completion_status")
        order = ReadingOrder.model_validate(json.loads(body))
        if order.base_content_revision_id != content.content_revision_id:
            raise ResponseContractError("阅读顺序目标修订不匹配", "base_content_revision_id")
        if set(order.block_ids) != {block.block_id for block in content.blocks}:
            raise ResponseContractError("阅读顺序必须完整引用已有块，不能新增或删除内容", "block_ids")
        return order


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


class ResponsesClient(ContentClientV2):
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

    def _v2_payload(self, model: str, input_value: list[dict[str, Any]],
                    name: str, schema: dict[str, Any]) -> dict[str, Any]:
        if not model.strip():
            raise ModelServiceError("识别模型名称不能为空", configuration_error=True,
                                    reason_category="service_configuration", field_path="model")
        return self._page_payload(model, input_value, name, schema)

    async def _v2_send(self, model: str, payload: dict[str, Any]) -> ProviderResponse:
        return await self._send(payload)

    def _v2_text(self, packet: ProviderResponse) -> ProviderText:
        data = packet.data
        texts: list[str] = []
        refused = False
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
                    refused = refused or part.get("type") == "refusal" or bool(part.get("refusal"))
                    if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                        texts.append(part["text"])
        if not texts and isinstance(data.get("output_text"), str):
            texts.append(data["output_text"])
        body = "".join(texts).strip()
        if refused:
            return ProviderText(body or packet.raw_body, "incomplete", "unreadable_source",
                                "模型拒绝处理本次内容；已保存响应", "output.refusal")
        status = data.get("status")
        if status == "incomplete":
            detail = data.get("incomplete_details")
            reason = detail.get("reason") if isinstance(detail, dict) else None
            return ProviderText(body or packet.raw_body, "truncated" if reason == "max_output_tokens" else "incomplete",
                                "truncated_response" if reason == "max_output_tokens" else "invalid_structure",
                                "模型响应未完整完成；已保存收到的正文", "incomplete_details.reason")
        if status != "completed":
            return ProviderText(body or packet.raw_body, "unknown", "invalid_structure",
                                "模型响应没有明确的完成状态；已保存响应", "status")
        if not body:
            return ProviderText(packet.raw_body, "incomplete", "invalid_structure",
                                "模型响应没有内容正文；已保存服务响应信封", "output")
        return ProviderText(body, "complete")

    async def recognize_page(
        self, model: str, input_value: list[dict[str, Any]], *,
        on_attempt_start: AttemptStart, on_attempt_end: AttemptEnd, retry: bool = False,
    ) -> StructuredPageResult:
        """Explicit compatibility for the layout-based V1 workflow."""
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
        """Explicit compatibility for the layout-based V1 workflow."""
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
        """Explicit compatibility for the layout-based V1 workflow."""
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
