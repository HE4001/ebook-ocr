"""Lightweight projections for polling and paginated workflow views."""
from __future__ import annotations

import json
import sqlite3

from .models import PageOutcome, PageOutcomeSummary, RunCounts, RunSummary, Usage, WorkflowError

_CATEGORY = """CASE
 WHEN json_extract(task_json,'$.outcome') IS NULL THEN NULL
 WHEN NOT coalesce(json_extract(task_json,'$.outcome.source_readable'),1) THEN 'no_result'
 WHEN json_extract(task_json,'$.outcome.source_disposition')='page_preserved' THEN 'page_preserved'
 WHEN json_extract(task_json,'$.outcome.source_disposition')='regions_preserved' THEN 'regions_preserved'
 WHEN coalesce(json_extract(task_json,'$.outcome.content_revision_id'),'')!=''
      AND json_extract(task_json,'$.outcome.content')='usable' THEN 'editable'
 WHEN coalesce(json_extract(task_json,'$.outcome.content_revision_id'),'')!=''
      AND coalesce(json_extract(task_json,'$.outcome.content'),'unverified') IN ('uncertain','unverified') THEN 'partial_content'
 ELSE 'no_result' END"""


def run_outcomes(connection: sqlite3.Connection, run_id: str, offset: int, limit: int) -> list[PageOutcomeSummary]:
    rows = connection.execute(
        "SELECT page_id,json_extract(task_json,'$.page_number') AS page_number,"
        "json_extract(task_json,'$.position') AS position,json_extract(task_json,'$.stage') AS stage,"
        "json_extract(task_json,'$.state') AS state,json_extract(task_json,'$.outcome') AS outcome,"
        "CASE WHEN json_extract(task_json,'$.outcome.protected_existing') THEN 'protected_existing' "
        f"ELSE coalesce({_CATEGORY},'no_result') END AS category FROM workflow_tasks WHERE run_id=? "
        "ORDER BY json_extract(task_json,'$.position'),page_id LIMIT ? OFFSET ?", (run_id, min(limit, 500), offset),
    ).fetchall()
    return [PageOutcomeSummary(**{key: row[key] for key in ('page_id','page_number','position','stage','state','category')},
                               outcome=PageOutcome.model_validate_json(row['outcome']) if row['outcome'] else None)
            for row in rows]


def run_summary(connection: sqlite3.Connection, book_id: str, run_id: str) -> RunSummary | None:
    row = connection.execute(
        "SELECT run_id,book_id,status,request_limit,request_count,updated_at,error,"
        "coalesce(json_extract(snapshot_json,'$.workflow_version'),1) AS workflow_version,"
        "coalesce(json_extract(snapshot_json,'$.recognition_scope'),'legacy_all_visible') AS recognition_scope,"
        "json_extract(snapshot_json,'$.selection_revision') AS selection_revision,"
        "json_extract(snapshot_json,'$.output_snapshot_id') AS output_snapshot_id "
        "FROM workflow_runs WHERE book_id=? AND run_id=?", (book_id, run_id),
    ).fetchone()
    if row is None:
        return None
    projections = {
        'completed': "outcome IS NOT NULL" if row['workflow_version'] == 2 else "result_status IS NOT NULL",
        **{name: f"result_status='{name}'" for name in ('auto_passed','completed_with_issues','failed')},
        **{name: f"category='{name}'" for name in ('editable','partial_content','regions_preserved','page_preserved','no_result')},
        'protected_existing': 'protected_existing=1',
    }
    counts = connection.execute(
        "WITH tasks AS (SELECT json_extract(task_json,'$.outcome') AS outcome,"
        "json_extract(task_json,'$.result_status') AS result_status,"
        "json_extract(task_json,'$.outcome.protected_existing') AS protected_existing,"
        f"{_CATEGORY} AS category FROM workflow_tasks WHERE run_id=?) SELECT count(*) AS total,"
        + ','.join(f'coalesce(sum({expression}),0) AS {name}' for name, expression in projections.items())
        + ' FROM tasks', (run_id,),
    ).fetchone()
    active = connection.execute(
        "SELECT json_extract(task_json,'$.stage') AS stage,count(*) AS count FROM workflow_tasks "
        "WHERE run_id=? AND json_extract(task_json,'$.state')='running' GROUP BY stage", (run_id,),
    ).fetchall()
    errors = connection.execute(
        "SELECT error.value FROM workflow_tasks AS task,json_each(task.task_json,'$.errors') AS error "
        "WHERE task.run_id=? ORDER BY json_extract(task.task_json,'$.position') DESC,task.page_id DESC,"
        "CAST(error.key AS INTEGER) DESC LIMIT 5", (run_id,),
    ).fetchall()
    usage = connection.execute(
        "WITH attempts AS (SELECT attempt_json FROM workflow_attempts WHERE run_id=? "
        "AND coalesce(json_extract(attempt_json,'$.state'),'reserved')!='cancelled') SELECT "
        "sum(json_extract(attempt_json,'$.usage.input_tokens')) AS input_tokens,"
        "sum(json_extract(attempt_json,'$.usage.output_tokens')) AS output_tokens,"
        "sum(json_extract(attempt_json,'$.usage.total_tokens')) AS total_tokens,"
        "count(*)>0 AND coalesce(min(coalesce(json_extract(attempt_json,'$.usage.complete'),0) "
        "AND coalesce(json_extract(attempt_json,'$.state'),'reserved') NOT IN ('reserved','unknown')),0) AS complete "
        "FROM attempts", (run_id,),
    ).fetchone()
    waiting = connection.execute(
        "SELECT min(json_extract(attempt_json,'$.sent_at')) FROM workflow_attempts WHERE run_id=? "
        "AND json_extract(attempt_json,'$.settled_at') IS NULL "
        "AND coalesce(json_extract(attempt_json,'$.state'),'reserved') IN ('reserved','succeeded')", (run_id,),
    ).fetchone()[0]
    formats = connection.execute(
        "SELECT json_extract(manifest_json,'$.formats') FROM export_manifests WHERE book_id=? "
        "AND manifest_id=? AND json_extract(manifest_json,'$.workflow_version')=2",
        (book_id, row['output_snapshot_id']),
    ).fetchone() if row['output_snapshot_id'] else None
    return RunSummary(**dict(row), counts=RunCounts(**dict(counts)), usage=Usage(**dict(usage)),
                      active_stages={item['stage']: item['count'] for item in active},
                      recent_errors=[WorkflowError.model_validate_json(item[0]) for item in reversed(errors)],
                      formats=json.loads(formats[0]) if formats and formats[0] else {}, model_wait_started_at=waiting)
