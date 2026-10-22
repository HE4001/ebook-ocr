"""Run state and helpers shared by both persisted workflow versions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .compile_service import CandidateRenderResult
from .layout_contract import PageSourceMetadata, SourceFidelityLayout
from .models import Book, PageTask, Revision, Run
from .quality_service import render_score
from .responses_client import CONFIGURATION_STATUS, _saved_http_status
from .source_analysis import SourceAnalysis


class _Paused(Exception):
    pass


class _ConfigurationStopped(Exception):
    pass


class _Unavailable(Exception):
    """A settled or unrepeatable operation has no usable persisted result."""

    def __init__(self, message: str, *, recorded: bool = False, category: str | None = None):
        super().__init__(message)
        self.recorded = recorded
        self.category = category


class _SourceUnavailable(Exception):
    """Source preparation actually failed to read the frozen file/page."""


@dataclass
class _RunContext:
    run: Run
    book: Book
    book_dir: Path
    pause_event: asyncio.Event | None
    first_recognized: asyncio.Event = field(default_factory=asyncio.Event)
    configuration_error: str | None = None
    style_samples: list[SourceFidelityLayout] = field(default_factory=list)
    style_summary: dict[str, Any] = field(default_factory=dict)
    prepared_pages: dict[str, tuple[dict[str, Any], Path, PageSourceMetadata, SourceAnalysis]] = field(default_factory=dict)


class WorkflowContext:
    """Shared storage access and run control; never owns a recognition stage."""

    def _task(self, context: _RunContext, page_id: str) -> PageTask:
        task = self.storage.get_task(context.run.run_id, page_id)
        if task is None:
            raise KeyError("task")
        return task

    def _pause_requested(self, context: _RunContext) -> bool:
        run = self.storage.get_run(context.run.book_id, context.run.run_id)
        return (context.pause_event is not None and context.pause_event.is_set()
                or run is not None and run.status in {"pausing", "paused"})

    def _checkpoint(self, context: _RunContext, *, source_only: bool = False) -> None:
        if self._pause_requested(context):
            raise _Paused()
        if context.configuration_error and not source_only:
            raise _ConfigurationStopped("模型服务已停止，当前请求未发送；保留已取得内容")

    def _update(self, context: _RunContext, page_id: str, **values: Any) -> PageTask:
        return self.storage.update_task(context.run.run_id, page_id, **values)

    def _revision(self, revision_id: str | None) -> Revision:
        revision = self.storage.get_revision(revision_id) if revision_id else None
        if revision is None:
            raise ValueError("阶段缺少已保存的候选修订")
        return revision

    def _restore_configuration_error(self, context: _RunContext) -> None:
        if context.configuration_error:
            return
        for attempt in self.storage.get_attempts(context.run.run_id):
            if _saved_http_status(attempt.error) in CONFIGURATION_STATUS:
                context.configuration_error = attempt.error
            elif context.run.workflow_version == 2:
                response = self.storage.get_recognition_response(attempt.attempt_id)
                error = next((item for item in response.parse_errors if item.category == "service_configuration"), None) if response else None
                if error:
                    context.configuration_error = error.message
            if context.configuration_error:
                self._update(context, attempt.page_id, stage_data={"configuration_error": context.configuration_error})
                return

    @staticmethod
    def _better_render(previous: CandidateRenderResult, candidate: CandidateRenderResult) -> bool:
        key = lambda item: (item.code, item.line_id, item.block_id)
        old = {key(item): item.severity for item in previous.diagnostics if item.severity != "info"}
        new = {key(item): item.severity for item in candidate.diagnostics if item.severity != "info"}
        return (render_score(candidate) < render_score(previous) and new.keys() <= old.keys()
                and not any(old[name] != "error" and severity == "error" for name, severity in new.items()))


def _safe_error(exc: Exception) -> str:
    return (str(exc).strip() or "页面处理发生技术错误")[:1000]
