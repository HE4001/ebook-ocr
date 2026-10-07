"""Wire-only content review and bounded repair proposals for automatic runs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .layout_contract import (
    BBox, EquationGroup, LayoutId, LayoutLine, LayoutRegion, NormalizedCoordinate,
)
from .content_contract import ContentConclusion, PageRegion, ScopeDisposition


class WorkflowModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ReviewIssue(WorkflowModel):
    category: str = Field(min_length=1, max_length=100)
    severity: Literal["warning", "error"]
    region_id: LayoutId | None
    line_id: LayoutId | None
    source_bbox: BBox | None
    reason: str = Field(min_length=1, max_length=2_000)
    repairable: bool


class PageReview(WorkflowModel):
    content: Literal["passed", "uncertain", "failed"]
    coverage: Literal["passed", "uncertain", "failed"]
    full_page_reviewed: bool
    issues: list[ReviewIssue] = Field(max_length=100)

    @model_validator(mode="after")
    def require_complete_review(self) -> "PageReview":
        if not self.full_page_reviewed:
            if self.content == "passed":
                self.content = "uncertain"
            if self.coverage == "passed":
                self.coverage = "uncertain"
        if self.issues:
            # A model cannot report unresolved differences and a clean pass.
            if self.content == "passed":
                self.content = "uncertain"
            if self.coverage == "passed":
                self.coverage = "uncertain"
        return self


class RepairEvidence(WorkflowModel):
    evidence_bbox: BBox
    reason: str = Field(min_length=1, max_length=2_000)


class ReplaceLine(RepairEvidence):
    op: Literal["replace_line"]
    line_id: LayoutId
    old_latex: str = Field(max_length=1_000_000)
    new_latex: str = Field(max_length=1_000_000)


class InsertRegion(RepairEvidence):
    op: Literal["insert_region"]
    after_region_id: LayoutId | None
    old_region_ids: list[LayoutId] = Field(max_length=500)
    region: LayoutRegion
    lines: list[LayoutLine] = Field(max_length=200)
    equation_groups: list[EquationGroup] = Field(max_length=100)

    @model_validator(mode="after")
    def require_local_observations(self) -> "InsertRegion":
        if any(line.block_id != self.region.region_id for line in self.lines):
            raise ValueError("插入块中的原行必须属于该新区域")
        observations = [self.region, *self.lines, *self.equation_groups]
        if any(item.basis not in {None, "model_estimate"} for item in observations) or any(
            line.style.basis not in {None, "model_estimate"} for line in self.lines
        ):
            raise ValueError("新增内容只能声明模型观察或未知依据")
        return self


class UpdateGeometry(RepairEvidence):
    op: Literal["update_geometry"]
    target_type: Literal["line", "region"]
    target_id: LayoutId
    old_bbox: BBox | None
    new_bbox: BBox | None
    old_baseline: NormalizedCoordinate | None
    new_baseline: NormalizedCoordinate | None

    @model_validator(mode="after")
    def region_has_no_baseline(self) -> "UpdateGeometry":
        if self.target_type == "region" and (
            self.old_baseline is not None or self.new_baseline is not None
        ):
            raise ValueError("区域几何不能包含原行基线")
        return self


class UpdateEquationGroup(RepairEvidence):
    op: Literal["update_equation_group"]
    group_id: LayoutId
    old_group: EquationGroup | None
    new_group: EquationGroup

    @model_validator(mode="after")
    def retain_group_identity(self) -> "UpdateEquationGroup":
        if self.new_group.group_id != self.group_id or (
            self.old_group is not None and self.old_group.group_id != self.group_id
        ):
            raise ValueError("公式组修复必须保留目标 ID")
        if self.new_group.basis not in {None, "model_estimate"}:
            raise ValueError("修改后的公式组只能声明模型观察或未知依据")
        return self


class DeleteDuplicateRegion(RepairEvidence):
    op: Literal["delete_duplicate_region"]
    region_id: LayoutId
    old_region: LayoutRegion
    old_lines: list[LayoutLine] = Field(max_length=200)
    duplicate_of_region_id: LayoutId

    @model_validator(mode="after")
    def require_distinct_duplicate(self) -> "DeleteDuplicateRegion":
        if self.old_region.region_id != self.region_id or self.region_id == self.duplicate_of_region_id:
            raise ValueError("只能删除明确指向另一保留区域的重复块")
        return self


RepairOperation = ReplaceLine | InsertRegion | UpdateGeometry | UpdateEquationGroup | DeleteDuplicateRegion


class RepairProposal(WorkflowModel):
    base_revision_id: str = Field(min_length=1, max_length=128)
    operations: list[RepairOperation] = Field(max_length=40)

    @model_validator(mode="after")
    def reset_model_matching(self) -> "RepairProposal":
        for operation in self.operations:
            if isinstance(operation, InsertRegion):
                lines = operation.lines
            elif isinstance(operation, DeleteDuplicateRegion):
                lines = operation.old_lines
            else:
                continue
            for line in lines:
                line.match_status = "unknown"
        return self


class RegionPlan(WorkflowModel):
    schema_version: Literal[2] = 2
    regions: list[PageRegion] = Field(max_length=2_000)


class RegionVerdict(WorkflowModel):
    cleanliness_verified: bool = False
    region_id: str = Field(min_length=1, max_length=128)
    candidate_id: str = Field(min_length=1, max_length=128)
    content: ContentConclusion
    coverage: Literal["passed", "uncertain", "failed"]


class ObservationDisposition(WorkflowModel):
    observation_id: str = Field(min_length=1, max_length=128)
    scope_disposition: ScopeDisposition
    mapped_region_ids: list[str] = Field(max_length=100)


class ContentReviewIssue(WorkflowModel):
    category: Literal["small_text", "reading_order", "invalid_structure", "missing_content",
                      "equation", "table", "unreadable_source"]
    severity: Literal["warning", "error"]
    block_id: str | None = Field(default=None, max_length=128)
    source_bbox: BBox | None = None
    field_path: str | None = Field(default=None, max_length=300)
    reason: str = Field(min_length=1, max_length=2_000)
    repairable: bool = Field(strict=True)
    region_id: str | None = None
    stable_target_id: str | None = None

    @model_validator(mode="after")
    def require_source_evidence(self) -> "ContentReviewIssue":
        if self.block_id is None and self.source_bbox is None:
            raise ValueError("无候选块的复核问题必须提供源页证据框")
        if self.category == "missing_content" and self.source_bbox is None:
            raise ValueError("遗漏内容必须提供源页证据框")
        return self


class ContentReview(WorkflowModel):
    schema_version: Literal[2] = 2
    base_content_revision_id: str = Field(min_length=1, max_length=128)
    discovered_regions: list[PageRegion] = Field(default_factory=list, max_length=2_000)
    checked_region_ids: list[str] = Field(default_factory=list, max_length=2_000)
    region_verdicts: list[RegionVerdict] = Field(default_factory=list, max_length=2_000)
    observation_dispositions: list[ObservationDisposition] = Field(default_factory=list, max_length=2_000)
    content: ContentConclusion
    coverage: Literal["passed", "uncertain", "failed"]
    full_page_reviewed: bool = Field(strict=True)
    issues: list[ContentReviewIssue] = Field(max_length=200)

    @model_validator(mode="after")
    def prevent_unsupported_pass(self) -> "ContentReview":
        if not self.full_page_reviewed or self.issues:
            if self.content == "usable":
                self.content = "uncertain"
            if self.coverage == "passed":
                self.coverage = "uncertain"
        return self


class ReadingOrder(WorkflowModel):
    schema_version: Literal[2] = 2
    base_content_revision_id: str = Field(min_length=1, max_length=128)
    block_ids: list[str] = Field(max_length=2_000)
    status: Literal["observed", "uncertain"]
    reasons: list[str] = Field(max_length=100)

    @model_validator(mode="after")
    def require_unique_ids(self) -> "ReadingOrder":
        if len(self.block_ids) != len(set(self.block_ids)):
            raise ValueError("阅读顺序不能重复引用内容块")
        if self.reasons:
            self.status = "uncertain"
        return self
