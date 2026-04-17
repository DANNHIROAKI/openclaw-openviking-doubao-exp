from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .common import append_jsonl, ensure_dir, read_json, write_json, write_text

SAMPLE_INGEST_FIELDS = [
    "run_id",
    "group_id",
    "group_short_id",
    "group_label",
    "rerun_id",
    "sample_id",
    "sample_index",
    "sessions_ingested",
    "ingest_start_ts",
    "ingest_end_ts",
    "ingest_elapsed_ms",
    "ingest_gateway_input_tokens",
    "ingest_gateway_output_tokens",
    "ingest_gateway_total_tokens",
    "ingest_ov_internal_input_tokens",
    "ingest_ov_internal_output_tokens",
    "ingest_ov_internal_total_tokens",
    "ingest_input_tokens_total",
    "ingest_output_tokens_total",
    "ingest_total_tokens_total",
    "ov_barrier_wait_ms",
    "post_reset_quiet_wait_ms",
    "formal_usage_complete",
]

TASK_DIRECT_FIELDS = [
    "run_id",
    "group_id",
    "group_short_id",
    "group_label",
    "rerun_id",
    "sample_id",
    "sample_index",
    "case_uid",
    "qa_index",
    "category",
    "question",
    "gold_answer",
    "prediction",
    "judge_correct",
    "judge_label",
    "judge_reasoning_raw",
    "judge_model",
    "judge_prompt_version",
    "qa_start_ts",
    "qa_end_ts",
    "qa_elapsed_ms",
    "qa_retry_count",
    "qa_error_flag",
    "qa_gateway_input_tokens",
    "qa_gateway_output_tokens",
    "qa_gateway_total_tokens",
    "qa_ov_internal_input_tokens",
    "qa_ov_internal_output_tokens",
    "qa_ov_internal_total_tokens",
    "qa_input_tokens_direct",
    "qa_output_tokens_direct",
    "qa_total_tokens_direct",
    "gateway_model_id",
    "evidence",
    "judge_result_raw",
    "judge_usage_input_tokens",
    "judge_usage_output_tokens",
    "judge_usage_total_tokens",
]

TASK_AMORTIZED_FIELDS = TASK_DIRECT_FIELDS + [
    "alloc_ingest_input_tokens",
    "alloc_ingest_output_tokens",
    "alloc_ingest_total_tokens",
    "alloc_ingest_elapsed_ms",
    "task_input_tokens_amortized",
    "task_output_tokens_amortized",
    "task_total_tokens_amortized",
    "task_elapsed_ms_amortized",
    "allocation_method",
]

PER_TASK_SCHEMA_SECTIONS = {
    "task_metrics_direct": [
        ("run_id", "唯一 run 标识，对应一个 sample × group × rerun。"),
        ("group_id", "组 slug，例如 g1-ov-nomemory。"),
        ("group_short_id", "主文展示用短组号，例如 G1。"),
        ("rerun_id", "fresh rerun 编号。"),
        ("sample_id", "对话样本 ID。"),
        ("case_uid", "稳定唯一 task 键；若数据集未提供则按 sample/question 生成。"),
        ("category", "LoCoMo 类别（已排除 category 5）。"),
        ("judge_correct", "judge 最终布尔判定。"),
        ("qa_input_tokens_direct", "单题直接问答 input token，已含可观测的 OV 单题内部 token。"),
        ("qa_elapsed_ms", "单题 `/v1/responses` 端到端耗时，含自动重试等待，不含 judge。"),
        ("qa_retry_count", "该题自动重试次数。"),
        ("qa_error_flag", "该题请求阶段是否报错。"),
    ],
    "task_metrics_amortized": [
        ("alloc_ingest_input_tokens", "把 sample 共享 ingest input token 以整型余数分配法均摊到该题。"),
        ("alloc_ingest_elapsed_ms", "把 sample 共享 ingest 耗时以整型余数分配法均摊到该题。"),
        ("task_input_tokens_amortized", "单题直接问答 input token + 共享 ingest 分摊。"),
        ("task_elapsed_ms_amortized", "单题直接问答耗时 + 共享 ingest 分摊。"),
        ("allocation_method", "固定为 integer_remainder_even_split，用于保证组级总量可严格回算。"),
    ],
}


def _jsonify(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if value is None:
        return ""
    return str(value)


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _jsonify(row.get(field)) for field in fieldnames})


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    ensure_dir(path.parent)
    if path.exists():
        path.unlink()
    for row in rows:
        append_jsonl(path, row)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows


def observer_totals_from_snapshot(snapshot: dict[str, Any] | None) -> dict[str, int] | None:
    if not isinstance(snapshot, dict):
        return None
    parsed = snapshot.get("observer_vlm_parsed")
    if not isinstance(parsed, dict):
        return None
    try:
        return {
            "input_tokens": int(parsed.get("input_tokens_total", 0) or 0),
            "output_tokens": int(parsed.get("output_tokens_total", 0) or 0),
            "total_tokens": int(parsed.get("total_tokens_total", 0) or 0),
        }
    except Exception:
        return None


def diff_observer_totals(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, int] | None:
    before_totals = observer_totals_from_snapshot(before)
    after_totals = observer_totals_from_snapshot(after)
    if before_totals is None or after_totals is None:
        return None
    return {
        "input_tokens": max(0, after_totals["input_tokens"] - before_totals["input_tokens"]),
        "output_tokens": max(0, after_totals["output_tokens"] - before_totals["output_tokens"]),
        "total_tokens": max(0, after_totals["total_tokens"] - before_totals["total_tokens"]),
    }


def integer_even_split(total: int, count: int) -> list[int]:
    if count <= 0:
        return []
    base = total // count
    remainder = total % count
    return [base + (1 if idx < remainder else 0) for idx in range(count)]


def _sum_int(rows: list[dict[str, Any]], key: str) -> int:
    return sum(int(row.get(key, 0) or 0) for row in rows)


def _sum_nested_usage(rows: list[dict[str, Any]], key: str, item: str) -> int:
    total = 0
    for row in rows:
        value = row.get(key)
        if isinstance(value, dict):
            total += int(value.get(item, 0) or 0)
    return total


def materialize_run_metrics(
    *,
    run_id: str,
    group_id: str,
    group_short_id: str,
    group_label: str,
    rerun_id: int,
    sample_id: str,
    sample_index: int,
    ingest_result: dict[str, Any],
    qa_summary: dict[str, Any],
    judge_summary: dict[str, Any],
    ingest_stage: dict[str, Any],
    ov_snapshots: dict[str, Any] | None,
    metrics_root: Path,
) -> dict[str, Any]:
    ov_snapshots = ov_snapshots or {}
    ingest_ov = diff_observer_totals(
        ov_snapshots.get("pre_ingest"),
        ov_snapshots.get("post_ingest"),
    )
    judge_rows = judge_summary.get("grades", [])
    if not isinstance(judge_rows, list):
        raise RuntimeError("judge_summary.grades must be a list")
    direct_rows: list[dict[str, Any]] = []
    for record in judge_rows:
        if not isinstance(record, dict):
            continue
        gateway_usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
        ov_usage = record.get("ov_usage_delta") if isinstance(record.get("ov_usage_delta"), dict) else None
        qa_gateway_input = int(gateway_usage.get("input_tokens", 0) or 0)
        qa_gateway_output = int(gateway_usage.get("output_tokens", 0) or 0)
        qa_gateway_total = int(gateway_usage.get("total_tokens", qa_gateway_input + qa_gateway_output) or 0)
        qa_ov_input = int(ov_usage.get("input_tokens", 0) or 0) if ov_usage is not None else None
        qa_ov_output = int(ov_usage.get("output_tokens", 0) or 0) if ov_usage is not None else None
        qa_ov_total = int(ov_usage.get("total_tokens", 0) or 0) if ov_usage is not None else None
        direct_rows.append(
            {
                "run_id": run_id,
                "group_id": group_id,
                "group_short_id": group_short_id,
                "group_label": group_label,
                "rerun_id": rerun_id,
                "sample_id": sample_id,
                "sample_index": sample_index,
                "case_uid": record.get("case_uid"),
                "qa_index": int(record.get("qa_index", record.get("qi", 0)) or 0),
                "category": str(record.get("category", "")),
                "question": str(record.get("question", "")),
                "gold_answer": str(record.get("gold_answer", record.get("expected", ""))),
                "prediction": str(record.get("prediction", record.get("response", ""))),
                "judge_correct": bool(record.get("judge_correct", record.get("grade"))),
                "judge_label": str(record.get("judge_label", "")),
                "judge_reasoning_raw": str(record.get("judge_reasoning_raw", record.get("judge_reasoning", ""))),
                "judge_model": str(record.get("judge_model", "")),
                "judge_prompt_version": str(record.get("judge_prompt_version", "")),
                "qa_start_ts": record.get("qa_start_ts"),
                "qa_end_ts": record.get("qa_end_ts"),
                "qa_elapsed_ms": int(record.get("qa_elapsed_ms", 0) or 0),
                "qa_retry_count": int(record.get("qa_retry_count", 0) or 0),
                "qa_error_flag": bool(record.get("qa_error_flag", record.get("error"))),
                "qa_gateway_input_tokens": qa_gateway_input,
                "qa_gateway_output_tokens": qa_gateway_output,
                "qa_gateway_total_tokens": qa_gateway_total,
                "qa_ov_internal_input_tokens": qa_ov_input,
                "qa_ov_internal_output_tokens": qa_ov_output,
                "qa_ov_internal_total_tokens": qa_ov_total,
                "qa_input_tokens_direct": qa_gateway_input + (qa_ov_input or 0),
                "qa_output_tokens_direct": qa_gateway_output + (qa_ov_output or 0),
                "qa_total_tokens_direct": qa_gateway_total + (qa_ov_total or 0),
                "gateway_model_id": str(record.get("gateway_model_id", "")),
                "evidence": record.get("evidence", []),
                "judge_result_raw": record.get("judge_result_raw", record.get("judge_parsed_json", {})),
                "judge_usage_input_tokens": int(record.get("judge_usage", {}).get("input_tokens", 0) or 0),
                "judge_usage_output_tokens": int(record.get("judge_usage", {}).get("output_tokens", 0) or 0),
                "judge_usage_total_tokens": int(record.get("judge_usage", {}).get("total_tokens", 0) or 0),
            }
        )
    direct_rows.sort(key=lambda row: (int(row.get("qa_index", 0)), str(row.get("case_uid", ""))))

    task_count = len(direct_rows)
    if task_count == 0:
        raise RuntimeError(f"No task rows materialized for {run_id}")

    ingest_gateway_input = int(ingest_result.get("usage_total", {}).get("input_tokens", 0) or 0)
    ingest_gateway_output = int(ingest_result.get("usage_total", {}).get("output_tokens", 0) or 0)
    ingest_gateway_total = int(ingest_result.get("usage_total", {}).get("total_tokens", 0) or 0)
    ingest_ov_input = int(ingest_ov["input_tokens"]) if isinstance(ingest_ov, dict) else 0
    ingest_ov_output = int(ingest_ov["output_tokens"]) if isinstance(ingest_ov, dict) else 0
    ingest_ov_total = int(ingest_ov["total_tokens"]) if isinstance(ingest_ov, dict) else 0
    ingest_input_total = ingest_gateway_input + ingest_ov_input
    ingest_output_total = ingest_gateway_output + ingest_ov_output
    ingest_total_total = ingest_gateway_total + ingest_ov_total

    ov_ingest_usage_observable = not (
        group_id != "g2-noov-stock"
        and isinstance(ov_snapshots.get("pre_ingest"), dict)
        and isinstance(ov_snapshots.get("post_ingest"), dict)
        and ingest_ov is None
    )

    sample_ingest_row = {
        "run_id": run_id,
        "group_id": group_id,
        "group_short_id": group_short_id,
        "group_label": group_label,
        "rerun_id": rerun_id,
        "sample_id": sample_id,
        "sample_index": sample_index,
        "sessions_ingested": int(ingest_result.get("session_count", len(ingest_result.get("results", []))) or 0),
        "ingest_start_ts": ingest_stage.get("ingest_start_ts"),
        "ingest_end_ts": ingest_stage.get("ingest_end_ts"),
        "ingest_elapsed_ms": int(ingest_stage.get("ingest_elapsed_ms", 0) or 0),
        "ingest_gateway_input_tokens": ingest_gateway_input,
        "ingest_gateway_output_tokens": ingest_gateway_output,
        "ingest_gateway_total_tokens": ingest_gateway_total,
        "ingest_ov_internal_input_tokens": ingest_ov_input,
        "ingest_ov_internal_output_tokens": ingest_ov_output,
        "ingest_ov_internal_total_tokens": ingest_ov_total,
        "ingest_input_tokens_total": ingest_input_total,
        "ingest_output_tokens_total": ingest_output_total,
        "ingest_total_tokens_total": ingest_total_total,
        "ov_barrier_wait_ms": int(ingest_stage.get("ov_barrier_wait_ms", 0) or 0),
        "post_reset_quiet_wait_ms": int(ingest_stage.get("post_reset_quiet_wait_ms", 0) or 0),
        "formal_usage_complete": bool(
            ingest_stage.get("formal_usage_complete", True)
            and ov_ingest_usage_observable
            and not any(row.get("qa_ov_internal_input_tokens") is None for row in direct_rows if row.get("group_id") != "g2-noov-stock")
        ),
    }

    alloc_ingest_input = integer_even_split(ingest_input_total, task_count)
    alloc_ingest_output = integer_even_split(ingest_output_total, task_count)
    alloc_ingest_total = integer_even_split(ingest_total_total, task_count)
    alloc_ingest_elapsed = integer_even_split(int(sample_ingest_row["ingest_elapsed_ms"]), task_count)

    amortized_rows: list[dict[str, Any]] = []
    for idx, row in enumerate(direct_rows):
        amortized_rows.append(
            {
                **row,
                "alloc_ingest_input_tokens": alloc_ingest_input[idx],
                "alloc_ingest_output_tokens": alloc_ingest_output[idx],
                "alloc_ingest_total_tokens": alloc_ingest_total[idx],
                "alloc_ingest_elapsed_ms": alloc_ingest_elapsed[idx],
                "task_input_tokens_amortized": int(row["qa_input_tokens_direct"]) + alloc_ingest_input[idx],
                "task_output_tokens_amortized": int(row["qa_output_tokens_direct"]) + alloc_ingest_output[idx],
                "task_total_tokens_amortized": int(row["qa_total_tokens_direct"]) + alloc_ingest_total[idx],
                "task_elapsed_ms_amortized": int(row["qa_elapsed_ms"]) + alloc_ingest_elapsed[idx],
                "allocation_method": "integer_remainder_even_split",
            }
        )

    validation = {
        "task_count": task_count,
        "qa_count_matches_judge": task_count == int(qa_summary.get("qa_count", task_count) or 0),
        "gateway_input_reconciles": _sum_int(direct_rows, "qa_gateway_input_tokens")
        == int(qa_summary.get("usage_total", {}).get("input_tokens", 0) or 0),
        "amortized_input_reconciles": _sum_int(amortized_rows, "task_input_tokens_amortized")
        == ingest_input_total + _sum_int(direct_rows, "qa_input_tokens_direct"),
        "amortized_elapsed_reconciles": _sum_int(amortized_rows, "task_elapsed_ms_amortized")
        == int(sample_ingest_row["ingest_elapsed_ms"]) + _sum_int(direct_rows, "qa_elapsed_ms"),
    }
    validation["all_pass"] = all(validation.values())

    by_run_root = metrics_root / "by_run"
    sample_file = by_run_root / "sample_ingest" / f"{run_id}.json"
    direct_file = by_run_root / "task_direct" / f"{run_id}.jsonl"
    amortized_file = by_run_root / "task_amortized" / f"{run_id}.jsonl"
    write_json(sample_file, sample_ingest_row)
    _write_jsonl(direct_file, direct_rows)
    _write_jsonl(amortized_file, amortized_rows)

    return {
        "sample_ingest_row": sample_ingest_row,
        "task_direct_rows": direct_rows,
        "task_amortized_rows": amortized_rows,
        "validation": validation,
        "paths": {
            "sample_ingest": str(sample_file),
            "task_direct": str(direct_file),
            "task_amortized": str(amortized_file),
        },
    }


def load_run_metric_rows(metrics_root: Path, metric_name: str) -> list[dict[str, Any]]:
    root = metrics_root / "by_run" / metric_name
    rows: list[dict[str, Any]] = []
    if not root.exists():
        return rows
    for path in sorted(root.glob("*")):
        if metric_name == "sample_ingest":
            value = read_json(path, default={})
            if isinstance(value, dict):
                rows.append(value)
        else:
            rows.extend(_read_jsonl(path))
    return rows


def rebuild_metric_exports(metrics_root: Path) -> dict[str, int]:
    sample_rows = load_run_metric_rows(metrics_root, "sample_ingest")
    direct_rows = load_run_metric_rows(metrics_root, "task_direct")
    amortized_rows = load_run_metric_rows(metrics_root, "task_amortized")

    for metric_name, rows, fields in [
        ("sample_ingest", sample_rows, SAMPLE_INGEST_FIELDS),
        ("task_direct", direct_rows, TASK_DIRECT_FIELDS),
        ("task_amortized", amortized_rows, TASK_AMORTIZED_FIELDS),
    ]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            group_id = str(row.get("group_id", "unknown"))
            grouped.setdefault(group_id, []).append(row)
        metric_dir = metrics_root / metric_name
        ensure_dir(metric_dir)
        for group_id, group_rows in grouped.items():
            _write_csv(metric_dir / f"{group_id}.csv", group_rows, fields)
        if metric_name == "task_direct":
            _write_csv(metrics_root / "task_metrics_direct_all_groups.csv", rows, fields)
        elif metric_name == "task_amortized":
            _write_csv(metrics_root / "task_metrics_amortized_all_groups.csv", rows, fields)
        elif metric_name == "sample_ingest":
            _write_csv(metrics_root / "sample_ingest_all_groups.csv", rows, fields)

    return {
        "sample_ingest_rows": len(sample_rows),
        "task_direct_rows": len(direct_rows),
        "task_amortized_rows": len(amortized_rows),
    }


def write_per_task_schema(summary_root: Path) -> None:
    lines = ["# Per-task Schema", ""]
    for section, entries in PER_TASK_SCHEMA_SECTIONS.items():
        lines.append(f"## {section}")
        lines.append("")
        lines.append("| Field | Meaning |")
        lines.append("| --- | --- |")
        for field, meaning in entries:
            lines.append(f"| `{field}` | {meaning} |")
        lines.append("")
    write_text(summary_root / "per_task_schema.md", "\n".join(lines) + "\n")
