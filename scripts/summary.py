from __future__ import annotations

import argparse
import json
import math
import os
import random
from pathlib import Path
from statistics import median
from typing import Any

from .experiment_spec import GROUP_ORDER, GROUPS, PLANNED_COMPARISONS
from .metrics import rebuild_metric_exports, write_per_task_schema
from .common import read_json, write_json, write_text


def load_run_manifests(artifacts_root: Path) -> list[dict[str, Any]]:
    manifests_dir = artifacts_root / "manifests"
    if not manifests_dir.exists():
        return []
    manifests: list[dict[str, Any]] = []
    for path in sorted(manifests_dir.glob("*.json")):
        data = read_json(path, default={})
        if isinstance(data, dict):
            manifests.append(data)
    return manifests


def pct(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value * 100:.2f}%"


def num(value: int | float | None) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return "N/A"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.2f}"
    return f"{int(round(value)):,}"


def mean(values: list[int | float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def p90(values: list[int | float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(0.9 * len(ordered)) - 1))
    return ordered[index]


def _selected_rerun_ids(manifests: list[dict[str, Any]]) -> list[int]:
    reruns = sorted({int(item.get("rerun", item.get("rerun_id", 0)) or 0) for item in manifests if item.get("success")})
    return [item for item in reruns if item > 0]


def _formal_rerun_id(manifests: list[dict[str, Any]]) -> int:
    env_value = os.environ.get("EXP_SUMMARY_RERUN", "").strip()
    if env_value:
        return int(env_value)
    reruns = _selected_rerun_ids(manifests)
    return reruns[0] if reruns else 1


def _valid_manifest(item: dict[str, Any], rerun_id: int) -> bool:
    if not item.get("success"):
        return False
    item_rerun = int(item.get("rerun", item.get("rerun_id", 0)) or 0)
    if item_rerun != rerun_id:
        return False
    if "formal_valid" in item:
        return bool(item.get("formal_valid"))
    return True


def _run_ids_for_rerun(manifests: list[dict[str, Any]], rerun_id: int) -> set[str]:
    return {str(item.get("run_id")) for item in manifests if _valid_manifest(item, rerun_id)}


def _load_metric_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    if path.suffix == ".json":
        value = read_json(path, default={})
        if isinstance(value, dict):
            rows.append(value)
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


def _load_rows_by_run(metrics_root: Path, metric_name: str, run_ids: set[str]) -> list[dict[str, Any]]:
    root = metrics_root / "by_run" / metric_name
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("*")):
        if path.stem not in run_ids:
            continue
        rows.extend(_load_metric_rows(path))
    return rows


def aggregate_group_rows(
    *,
    sample_rows: list[dict[str, Any]],
    direct_rows: list[dict[str, Any]],
    amortized_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {}
    for group_id in GROUP_ORDER:
        meta = GROUPS[group_id]
        grouped[group_id] = {
            "group_id": group_id,
            "meta": meta,
            "run_count": 0,
            "task_count": 0,
            "correct": 0,
            "input_tokens_total": 0,
            "elapsed_ms_total": 0,
            "total_tokens_total": 0,
            "direct_qa_elapsed_values": [],
            "amortized_elapsed_values": [],
            "sample_metrics": {},
            "category_metrics": {},
            "qa_error_count": 0,
            "retry_task_count": 0,
            "retry_count_total": 0,
        }

    for row in sample_rows:
        group_id = str(row.get("group_id"))
        if group_id not in grouped:
            continue
        grouped[group_id]["run_count"] += 1

    direct_by_group_sample: dict[tuple[str, str], list[dict[str, Any]]] = {}
    amortized_by_group_sample: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in direct_rows:
        group_id = str(row.get("group_id"))
        sample_id = str(row.get("sample_id"))
        direct_by_group_sample.setdefault((group_id, sample_id), []).append(row)
    for row in amortized_rows:
        group_id = str(row.get("group_id"))
        sample_id = str(row.get("sample_id"))
        amortized_by_group_sample.setdefault((group_id, sample_id), []).append(row)

    for group_id in GROUP_ORDER:
        bucket = grouped[group_id]
        group_direct = [row for row in direct_rows if str(row.get("group_id")) == group_id]
        group_amortized = [row for row in amortized_rows if str(row.get("group_id")) == group_id]
        bucket["task_count"] = len(group_direct)
        bucket["correct"] = sum(1 for row in group_direct if row.get("judge_correct"))
        bucket["input_tokens_total"] = sum(int(row.get("task_input_tokens_amortized", 0) or 0) for row in group_amortized)
        bucket["elapsed_ms_total"] = sum(int(row.get("task_elapsed_ms_amortized", 0) or 0) for row in group_amortized)
        bucket["total_tokens_total"] = sum(int(row.get("task_total_tokens_amortized", 0) or 0) for row in group_amortized)
        bucket["direct_qa_elapsed_values"] = [int(row.get("qa_elapsed_ms", 0) or 0) for row in group_direct]
        bucket["amortized_elapsed_values"] = [int(row.get("task_elapsed_ms_amortized", 0) or 0) for row in group_amortized]
        bucket["qa_error_count"] = sum(1 for row in group_direct if row.get("qa_error_flag"))
        bucket["retry_task_count"] = sum(1 for row in group_direct if int(row.get("qa_retry_count", 0) or 0) > 0)
        bucket["retry_count_total"] = sum(int(row.get("qa_retry_count", 0) or 0) for row in group_direct)
        bucket["task_completion_rate"] = bucket["correct"] / bucket["task_count"] if bucket["task_count"] else 0.0
        bucket["direct_qa_avg_elapsed_ms"] = mean(bucket["direct_qa_elapsed_values"])
        bucket["direct_qa_median_elapsed_ms"] = median(bucket["direct_qa_elapsed_values"]) if bucket["direct_qa_elapsed_values"] else None
        bucket["direct_qa_p90_elapsed_ms"] = p90(bucket["direct_qa_elapsed_values"])
        bucket["amortized_avg_elapsed_ms"] = mean(bucket["amortized_elapsed_values"])
        bucket["amortized_median_elapsed_ms"] = median(bucket["amortized_elapsed_values"]) if bucket["amortized_elapsed_values"] else None
        bucket["amortized_p90_elapsed_ms"] = p90(bucket["amortized_elapsed_values"])
        bucket["cost_per_correct"] = (
            bucket["input_tokens_total"] / bucket["correct"] if bucket["correct"] else None
        )
        bucket["elapsed_per_correct"] = (
            bucket["elapsed_ms_total"] / bucket["correct"] if bucket["correct"] else None
        )
        bucket["api_error_rate"] = bucket["qa_error_count"] / bucket["task_count"] if bucket["task_count"] else 0.0
        bucket["retry_rate"] = bucket["retry_task_count"] / bucket["task_count"] if bucket["task_count"] else 0.0

        sample_ids = sorted({sample_id for (gid, sample_id) in direct_by_group_sample if gid == group_id})
        for sample_id in sample_ids:
            s_direct = direct_by_group_sample[(group_id, sample_id)]
            s_amortized = amortized_by_group_sample.get((group_id, sample_id), [])
            sample_task_count = len(s_direct)
            sample_correct = sum(1 for row in s_direct if row.get("judge_correct"))
            bucket["sample_metrics"][sample_id] = {
                "task_count": sample_task_count,
                "correct": sample_correct,
                "completion_rate": sample_correct / sample_task_count if sample_task_count else 0.0,
                "input_tokens_total": sum(int(row.get("task_input_tokens_amortized", 0) or 0) for row in s_amortized),
                "elapsed_ms_total": sum(int(row.get("task_elapsed_ms_amortized", 0) or 0) for row in s_amortized),
                "direct_qa_elapsed_total": sum(int(row.get("qa_elapsed_ms", 0) or 0) for row in s_direct),
                "direct_qa_avg_elapsed_ms": (
                    sum(int(row.get("qa_elapsed_ms", 0) or 0) for row in s_direct) / sample_task_count
                    if sample_task_count
                    else 0.0
                ),
                "amortized_avg_elapsed_ms": (
                    sum(int(row.get("task_elapsed_ms_amortized", 0) or 0) for row in s_amortized) / sample_task_count
                    if sample_task_count
                    else 0.0
                ),
            }

        categories = sorted({str(row.get("category")) for row in group_direct})
        for category in categories:
            c_direct = [row for row in group_direct if str(row.get("category")) == category]
            c_amortized = [row for row in group_amortized if str(row.get("category")) == category]
            total = len(c_direct)
            correct = sum(1 for row in c_direct if row.get("judge_correct"))
            bucket["category_metrics"][category] = {
                "task_count": total,
                "correct": correct,
                "completion_rate": correct / total if total else 0.0,
                "avg_amortized_input_tokens": (
                    sum(int(row.get("task_input_tokens_amortized", 0) or 0) for row in c_amortized) / total
                    if total
                    else 0.0
                ),
                "avg_amortized_elapsed_ms": (
                    sum(int(row.get("task_elapsed_ms_amortized", 0) or 0) for row in c_amortized) / total
                    if total
                    else 0.0
                ),
            }
    return grouped


def comparison_delta(control: dict[str, Any], treatment: dict[str, Any]) -> dict[str, Any]:
    control_rate = control["task_completion_rate"]
    treatment_rate = treatment["task_completion_rate"]
    control_tokens = control["input_tokens_total"]
    treatment_tokens = treatment["input_tokens_total"]
    control_elapsed = control["elapsed_ms_total"]
    treatment_elapsed = treatment["elapsed_ms_total"]
    return {
        "completion_rate_diff_pp": (treatment_rate - control_rate) * 100.0,
        "completion_rate_relative_pct": ((treatment_rate / control_rate) - 1.0) * 100.0 if control_rate else None,
        "input_token_diff": treatment_tokens - control_tokens,
        "input_token_drop_relative_pct": ((control_tokens - treatment_tokens) / control_tokens) * 100.0 if control_tokens else None,
        "elapsed_ms_diff": treatment_elapsed - control_elapsed,
        "direct_qa_avg_elapsed_ms_diff": (
            treatment["direct_qa_avg_elapsed_ms"] - control["direct_qa_avg_elapsed_ms"]
            if treatment["direct_qa_avg_elapsed_ms"] is not None and control["direct_qa_avg_elapsed_ms"] is not None
            else None
        ),
        "cost_per_correct_delta": (
            treatment["cost_per_correct"] - control["cost_per_correct"]
            if treatment["cost_per_correct"] is not None and control["cost_per_correct"] is not None
            else None
        ),
    }


def bootstrap_pair_ci(
    *,
    control: dict[str, Any],
    treatment: dict[str, Any],
    iterations: int = 3000,
    seed: int = 42,
) -> dict[str, Any]:
    sample_ids = sorted(set(control["sample_metrics"]).intersection(treatment["sample_metrics"]))
    rnd = random.Random(seed)
    if not sample_ids:
        return {
            "n_samples": 0,
            "completion_pp_ci95": None,
            "token_diff_ci95": None,
            "elapsed_ms_diff_ci95": None,
            "cost_per_correct_diff_ci95": None,
            "direct_qa_avg_elapsed_ms_diff_ci95": None,
            "amortized_avg_elapsed_ms_diff_ci95": None,
        }
    completion_diffs: list[float] = []
    token_diffs: list[float] = []
    elapsed_diffs: list[float] = []
    cost_diffs: list[float] = []
    direct_elapsed_diffs: list[float] = []
    amortized_elapsed_diffs: list[float] = []
    for _ in range(iterations):
        draw = [rnd.choice(sample_ids) for _ in range(len(sample_ids))]
        control_task_total = sum(control["sample_metrics"][sid]["task_count"] for sid in draw)
        treatment_task_total = sum(treatment["sample_metrics"][sid]["task_count"] for sid in draw)
        control_correct = sum(control["sample_metrics"][sid]["correct"] for sid in draw)
        treatment_correct = sum(treatment["sample_metrics"][sid]["correct"] for sid in draw)
        control_tokens = sum(control["sample_metrics"][sid]["input_tokens_total"] for sid in draw)
        treatment_tokens = sum(treatment["sample_metrics"][sid]["input_tokens_total"] for sid in draw)
        control_elapsed = sum(control["sample_metrics"][sid]["elapsed_ms_total"] for sid in draw)
        treatment_elapsed = sum(treatment["sample_metrics"][sid]["elapsed_ms_total"] for sid in draw)
        control_direct_elapsed = sum(control["sample_metrics"][sid]["direct_qa_elapsed_total"] for sid in draw)
        treatment_direct_elapsed = sum(treatment["sample_metrics"][sid]["direct_qa_elapsed_total"] for sid in draw)

        control_rate = control_correct / control_task_total if control_task_total else 0.0
        treatment_rate = treatment_correct / treatment_task_total if treatment_task_total else 0.0
        completion_diffs.append((treatment_rate - control_rate) * 100.0)
        token_diffs.append(treatment_tokens - control_tokens)
        elapsed_diffs.append(treatment_elapsed - control_elapsed)
        direct_elapsed_diffs.append(
            (treatment_direct_elapsed / treatment_task_total if treatment_task_total else 0.0)
            - (control_direct_elapsed / control_task_total if control_task_total else 0.0)
        )
        amortized_elapsed_diffs.append(
            (treatment_elapsed / treatment_task_total if treatment_task_total else 0.0)
            - (control_elapsed / control_task_total if control_task_total else 0.0)
        )
        if control_correct and treatment_correct:
            cost_diffs.append((treatment_tokens / treatment_correct) - (control_tokens / control_correct))
        else:
            cost_diffs.append(float("nan"))

    def interval(values: list[float]) -> list[float] | None:
        values = [value for value in values if not math.isnan(value)]
        if not values:
            return None
        values.sort()
        lo_idx = int(0.025 * len(values))
        hi_idx = max(lo_idx, int(0.975 * len(values)) - 1)
        return [values[lo_idx], values[hi_idx]]

    return {
        "n_samples": len(sample_ids),
        "completion_pp_ci95": interval(completion_diffs),
        "token_diff_ci95": interval(token_diffs),
        "elapsed_ms_diff_ci95": interval(elapsed_diffs),
        "cost_per_correct_diff_ci95": interval(cost_diffs),
        "direct_qa_avg_elapsed_ms_diff_ci95": interval(direct_elapsed_diffs),
        "amortized_avg_elapsed_ms_diff_ci95": interval(amortized_elapsed_diffs),
    }


def build_main_table(grouped: dict[str, Any]) -> str:
    lines = [
        "| 组 ID | 组别 | `plugins.slots.memory` | `plugins.slots.contextEngine` | `plugins.entries.openviking.enabled` | `plugins.deny` | 任务完成率 | 成本：输入 token（总计） | 总耗时（SUT） | 直接 QA 平均耗时 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for group_id in GROUP_ORDER:
        bucket = grouped[group_id]
        meta = bucket["meta"]
        lines.append(
            "| {short_id} | {label} | `{memory}` | `{context}` | `{enabled}` | `{deny}` | {rate} | {tokens} | {elapsed} | {qa_avg} |".format(
                short_id=meta["short_id"],
                label=meta["label"],
                memory=meta["plugins.slots.memory"],
                context=meta["plugins.slots.contextEngine"],
                enabled=str(meta["plugins.entries.openviking.enabled"]).lower(),
                deny=json.dumps(meta["plugins.deny"], ensure_ascii=False),
                rate=pct(bucket["task_completion_rate"]),
                tokens=num(bucket["input_tokens_total"]),
                elapsed=num(bucket["elapsed_ms_total"]),
                qa_avg=num(bucket["direct_qa_avg_elapsed_ms"]),
            )
        )
    return "\n".join(lines) + "\n"


def build_planned_comparisons(grouped: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    deltas: dict[str, Any] = {}
    bootstrap: dict[str, Any] = {}
    lines = [
        "| 比较 | 比较性质 | 完成率差值（pp） | 相对提升（%） | 输入 token 差值 | 输入 token 降幅（%） | 总耗时差值 | 直接 QA 平均耗时差值 | 每正确题成本变化 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in PLANNED_COMPARISONS:
        control = grouped[item["control_group"]]
        treatment = grouped[item["treatment_group"]]
        delta = comparison_delta(control, treatment)
        ci = bootstrap_pair_ci(control=control, treatment=treatment)
        deltas[item["comparison_id"]] = delta
        bootstrap[item["comparison_id"]] = ci
        lines.append(
            "| {label} | {nature} | {pp} | {rel} | {tok} | {drop} | {elapsed} | {qa} | {cpc} |".format(
                label=item["label"],
                nature=item["comparison_nature"],
                pp=f"{delta['completion_rate_diff_pp']:+.2f}",
                rel=("N/A" if delta["completion_rate_relative_pct"] is None else f"{delta['completion_rate_relative_pct']:+.2f}%"),
                tok=f"{delta['input_token_diff']:+,}",
                drop=("N/A" if delta["input_token_drop_relative_pct"] is None else f"{delta['input_token_drop_relative_pct']:+.2f}%"),
                elapsed=f"{int(delta['elapsed_ms_diff']):+,}",
                qa=("N/A" if delta["direct_qa_avg_elapsed_ms_diff"] is None else f"{delta['direct_qa_avg_elapsed_ms_diff']:+.2f}"),
                cpc=("N/A" if delta["cost_per_correct_delta"] is None else f"{delta['cost_per_correct_delta']:+.2f}"),
            )
        )
    lines.append("")
    lines.append("| 比较 | 聚类 sample 数 | 完成率差值 95% CI (pp) | 输入 token 差值 95% CI | 总耗时差值 95% CI | 每正确题成本差值 95% CI | 直接 QA 平均耗时差值 95% CI | 摊销后单题耗时差值 95% CI |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for item in PLANNED_COMPARISONS:
        ci = bootstrap[item["comparison_id"]]
        lines.append(
            "| {label} | {n} | {comp} | {tok} | {elapsed} | {cpc} | {qa} | {amort} |".format(
                label=item["comparison_id"],
                n=ci["n_samples"],
                comp=("N/A" if ci["completion_pp_ci95"] is None else f"[{ci['completion_pp_ci95'][0]:+.2f}, {ci['completion_pp_ci95'][1]:+.2f}]"),
                tok=("N/A" if ci["token_diff_ci95"] is None else f"[{ci['token_diff_ci95'][0]:+,.0f}, {ci['token_diff_ci95'][1]:+,.0f}]"),
                elapsed=("N/A" if ci["elapsed_ms_diff_ci95"] is None else f"[{ci['elapsed_ms_diff_ci95'][0]:+,.0f}, {ci['elapsed_ms_diff_ci95'][1]:+,.0f}]"),
                cpc=("N/A" if ci["cost_per_correct_diff_ci95"] is None else f"[{ci['cost_per_correct_diff_ci95'][0]:+.2f}, {ci['cost_per_correct_diff_ci95'][1]:+.2f}]"),
                qa=("N/A" if ci["direct_qa_avg_elapsed_ms_diff_ci95"] is None else f"[{ci['direct_qa_avg_elapsed_ms_diff_ci95'][0]:+.2f}, {ci['direct_qa_avg_elapsed_ms_diff_ci95'][1]:+.2f}]"),
                amort=("N/A" if ci["amortized_avg_elapsed_ms_diff_ci95"] is None else f"[{ci['amortized_avg_elapsed_ms_diff_ci95'][0]:+.2f}, {ci['amortized_avg_elapsed_ms_diff_ci95'][1]:+.2f}]"),
            )
        )
    return "\n".join(lines) + "\n", deltas, bootstrap


def build_sample_breakdown(grouped: dict[str, Any]) -> str:
    sample_ids = sorted({sample_id for bucket in grouped.values() for sample_id in bucket["sample_metrics"].keys()})
    lines = [
        "| `sample_id` | 题量 | G1 完成率 | G2 完成率 | G3 完成率 | G1 输入 token | G2 输入 token | G3 输入 token | G1 总耗时 | G2 总耗时 | G3 总耗时 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for sample_id in sample_ids:
        row = [f"`{sample_id}`"]
        task_count = next(
            (bucket["sample_metrics"][sample_id]["task_count"] for bucket in grouped.values() if sample_id in bucket["sample_metrics"]),
            0,
        )
        row.append(str(task_count))
        for group_id in GROUP_ORDER:
            metric = grouped[group_id]["sample_metrics"].get(sample_id, {})
            row.append(pct(metric.get("completion_rate")))
        for group_id in GROUP_ORDER:
            metric = grouped[group_id]["sample_metrics"].get(sample_id, {})
            row.append(num(metric.get("input_tokens_total")))
        for group_id in GROUP_ORDER:
            metric = grouped[group_id]["sample_metrics"].get(sample_id, {})
            row.append(num(metric.get("elapsed_ms_total")))
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines) + "\n"


def build_category_breakdown(grouped: dict[str, Any]) -> str:
    categories = sorted({cat for bucket in grouped.values() for cat in bucket["category_metrics"].keys()}, key=lambda value: int(value))
    lines = [
        "| Category | G1 完成率 | G2 完成率 | G3 完成率 | G1 平均单题摊销 input tokens | G2 平均单题摊销 input tokens | G3 平均单题摊销 input tokens | G1 平均单题摊销耗时 | G2 平均单题摊销耗时 | G3 平均单题摊销耗时 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for category in categories:
        row = [category]
        for group_id in GROUP_ORDER:
            row.append(pct(grouped[group_id]["category_metrics"].get(category, {}).get("completion_rate")))
        for group_id in GROUP_ORDER:
            row.append(num(grouped[group_id]["category_metrics"].get(category, {}).get("avg_amortized_input_tokens")))
        for group_id in GROUP_ORDER:
            row.append(num(grouped[group_id]["category_metrics"].get(category, {}).get("avg_amortized_elapsed_ms")))
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines) + "\n"


def build_latency_breakdown(grouped: dict[str, Any]) -> str:
    lines = [
        "| 组 ID | 直接 QA 平均耗时 | 直接 QA 中位数 | 直接 QA P90 | 摊销后单题平均耗时 | 摊销后单题中位数 | 摊销后单题 P90 | API 错误率 | 重试率 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for group_id in GROUP_ORDER:
        bucket = grouped[group_id]
        lines.append(
            "| {short_id} | {qa_avg} | {qa_med} | {qa_p90} | {am_avg} | {am_med} | {am_p90} | {err} | {retry} |".format(
                short_id=bucket["meta"]["short_id"],
                qa_avg=num(bucket["direct_qa_avg_elapsed_ms"]),
                qa_med=num(bucket["direct_qa_median_elapsed_ms"]),
                qa_p90=num(bucket["direct_qa_p90_elapsed_ms"]),
                am_avg=num(bucket["amortized_avg_elapsed_ms"]),
                am_med=num(bucket["amortized_median_elapsed_ms"]),
                am_p90=num(bucket["amortized_p90_elapsed_ms"]),
                err=pct(bucket["api_error_rate"]),
                retry=pct(bucket["retry_rate"]),
            )
        )
    return "\n".join(lines) + "\n"


def summarize(artifacts_root: Path) -> dict[str, Any]:
    metrics_root = artifacts_root / "metrics"
    rebuild_metric_exports(metrics_root)

    manifests = load_run_manifests(artifacts_root)
    formal_rerun_id = _formal_rerun_id(manifests)
    run_ids = _run_ids_for_rerun(manifests, formal_rerun_id)

    sample_rows = _load_rows_by_run(metrics_root, "sample_ingest", run_ids)
    direct_rows = _load_rows_by_run(metrics_root, "task_direct", run_ids)
    amortized_rows = _load_rows_by_run(metrics_root, "task_amortized", run_ids)
    grouped = aggregate_group_rows(sample_rows=sample_rows, direct_rows=direct_rows, amortized_rows=amortized_rows)

    summary_root = artifacts_root / "summary"
    summary_root.mkdir(parents=True, exist_ok=True)
    write_text(summary_root / "main_table.md", build_main_table(grouped))
    planned_md, comparison_deltas, comparison_bootstrap = build_planned_comparisons(grouped)
    write_text(summary_root / "planned_comparisons.md", planned_md)
    write_text(summary_root / "sample_breakdown.md", build_sample_breakdown(grouped))
    write_text(summary_root / "category_breakdown.md", build_category_breakdown(grouped))
    write_text(summary_root / "latency_breakdown.md", build_latency_breakdown(grouped))
    write_per_task_schema(summary_root)

    payload = {
        "formal_rerun_id": formal_rerun_id,
        "available_rerun_ids": _selected_rerun_ids(manifests),
        "run_ids": sorted(run_ids),
        "grouped": grouped,
        "planned_comparisons": comparison_deltas,
        "bootstrap": comparison_bootstrap,
        "counts": {
            "sample_ingest_rows": len(sample_rows),
            "task_direct_rows": len(direct_rows),
            "task_amortized_rows": len(amortized_rows),
        },
    }
    write_json(summary_root / "summary.json", payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize experiment artifacts.")
    parser.add_argument("--artifacts-root", type=Path, required=True)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    payload = summarize(args.artifacts_root)
    print(
        json.dumps(
            {
                "formal_rerun_id": payload["formal_rerun_id"],
                "groups": {
                    group_id: {
                        "task_completion_rate": payload["grouped"][group_id]["task_completion_rate"],
                        "input_tokens_total": payload["grouped"][group_id]["input_tokens_total"],
                        "elapsed_ms_total": payload["grouped"][group_id]["elapsed_ms_total"],
                    }
                    for group_id in GROUP_ORDER
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
