from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests

from .common import (
    append_jsonl,
    elapsed_ms,
    ensure_dir,
    iso_from_epoch,
    read_json,
    sha256_text,
    write_json,
)
from .experiment_spec import EXPECTED_CASE_COUNT, EXPECTED_SAMPLE_COUNT
from .openviking_probe import capture_vlm_snapshot

DEFAULT_REQUEST_TIMEOUT_SECONDS = float(os.environ.get("EXP_GATEWAY_REQUEST_TIMEOUT_S", "300") or "300")
DEFAULT_RESET_TIMEOUT_SECONDS = float(
    os.environ.get("EXP_RESET_TIMEOUT_S", os.environ.get("EXP_GATEWAY_REQUEST_TIMEOUT_S", "300"))
    or os.environ.get("EXP_GATEWAY_REQUEST_TIMEOUT_S", "300")
)


def format_locomo_message(msg: dict[str, Any]) -> str:
    speaker = msg.get("speaker", "unknown")
    text = msg.get("text", "")
    line = f"{speaker}: {text}"
    img_urls = msg.get("img_url", [])
    if isinstance(img_urls, str):
        img_urls = [img_urls]
    blip = msg.get("blip_caption", "")
    if img_urls:
        for url in img_urls:
            caption = f": {blip}" if blip else ""
            line += f"\n{url}{caption}"
    elif blip:
        line += f"\n({blip})"
    return line


def load_locomo_data(path: Path, sample_index: int | None = None) -> list[dict[str, Any]]:
    data = read_json(path, default=[])
    if not isinstance(data, list):
        raise RuntimeError(f"Dataset is not a list: {path}")
    if sample_index is None:
        return data
    if sample_index < 0 or sample_index >= len(data):
        raise RuntimeError(f"Sample index {sample_index} out of range for {path}")
    return [data[sample_index]]


def canonical_sample_id(sample: dict[str, Any], sample_index: int) -> str:
    raw = str(sample.get("sample_id", "") or "").strip()
    return raw or f"sample{sample_index + 1:02d}"


def case_uid_for_qa(*, sample_id: str, qa: dict[str, Any], qa_index: int) -> str:
    for key in ("case_uid", "case_id", "qa_id", "qid", "id"):
        value = qa.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    basis = "|".join(
        [
            sample_id,
            str(qa_index),
            str(qa.get("category", "")),
            str(qa.get("question", "")),
            str(qa.get("answer", "")),
        ]
    )
    return f"{sample_id}__q{qa_index:03d}__{sha256_text(basis)[:12]}"


def iter_formal_qas(sample: dict[str, Any], *, sample_index: int) -> list[dict[str, Any]]:
    sample_id = canonical_sample_id(sample, sample_index)
    formal_qas: list[dict[str, Any]] = []
    for qa_index, qa in enumerate(sample.get("qa", []), start=1):
        category = str(qa.get("category", ""))
        if category == "5":
            continue
        formal_qas.append(
            {
                "sample_id": sample_id,
                "sample_index": sample_index,
                "qa_index": qa_index,
                "case_uid": case_uid_for_qa(sample_id=sample_id, qa=qa, qa_index=qa_index),
                "question": str(qa.get("question", "")),
                "gold_answer": str(qa.get("answer", "")),
                "category": category,
                "evidence": qa.get("evidence", []),
                "raw": qa,
            }
        )
    return formal_qas


def validate_dataset(path: Path) -> dict[str, Any]:
    samples = load_locomo_data(path)
    qa_total_raw = 0
    qa_non_cat5 = 0
    qa_cat5 = 0
    cat_counts: dict[str, int] = {}
    sample_question_counts: dict[str, int] = {}
    missing_sample_ids: list[int] = []
    duplicate_case_uids: list[str] = []
    seen_case_uids: set[str] = set()
    for sample_index, sample in enumerate(samples):
        sample_id = canonical_sample_id(sample, sample_index)
        if not str(sample.get("sample_id", "") or "").strip():
            missing_sample_ids.append(sample_index)
        formal_count = 0
        for qa_index, qa in enumerate(sample.get("qa", []), start=1):
            cat = str(qa.get("category", ""))
            qa_total_raw += 1
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
            if cat == "5":
                qa_cat5 += 1
                continue
            qa_non_cat5 += 1
            formal_count += 1
            case_uid = case_uid_for_qa(sample_id=sample_id, qa=qa, qa_index=qa_index)
            if case_uid in seen_case_uids:
                duplicate_case_uids.append(case_uid)
            seen_case_uids.add(case_uid)
        sample_question_counts[sample_id] = formal_count
    report = {
        "samples": len(samples),
        "qa_total_raw": qa_total_raw,
        "qa_non_category5": qa_non_cat5,
        "qa_category5": qa_cat5,
        "category_counts": cat_counts,
        "sample_question_counts": sample_question_counts,
        "missing_sample_ids": missing_sample_ids,
        "case_uid_count": len(seen_case_uids),
        "duplicate_case_uid_count": len(duplicate_case_uids),
        "duplicate_case_uids_preview": duplicate_case_uids[:20],
        "is_expected_locomo10": len(samples) == EXPECTED_SAMPLE_COUNT,
        "is_expected_1540": qa_non_cat5 == EXPECTED_CASE_COUNT,
        "category5_zero": qa_cat5 == 0,
        "sample_ids_complete": not missing_sample_ids,
        "case_uid_unique": not duplicate_case_uids,
    }
    report["valid_formal_dataset"] = bool(
        report["is_expected_locomo10"]
        and report["is_expected_1540"]
        and report["category5_zero"]
        and report["sample_ids_complete"]
        and report["case_uid_unique"]
    )
    return report


def build_session_messages(
    item: dict[str, Any],
    *,
    tail: str,
    session_range: tuple[int, int] | None = None,
) -> list[dict[str, Any]]:
    conv = item["conversation"]
    speakers = f"{conv['speaker_a']} & {conv['speaker_b']}"
    session_keys = sorted(
        [k for k in conv if k.startswith("session_") and not k.endswith("_date_time")],
        key=lambda k: int(k.split("_")[1]),
    )
    output: list[dict[str, Any]] = []
    sample_id = canonical_sample_id(item, 0)
    for session_key in session_keys:
        sess_num = int(session_key.split("_")[1])
        if session_range is not None:
            lo, hi = session_range
            if sess_num < lo or sess_num > hi:
                continue
        date_time = conv.get(f"{session_key}_date_time", "")
        parts = [f"[group chat conversation: {date_time}]"]
        for msg in conv[session_key]:
            parts.append(format_locomo_message(msg))
        if tail:
            parts.append(tail)
        output.append(
            {
                "message": "\n\n".join(parts),
                "meta": {
                    "sample_id": sample_id,
                    "session_key": session_key,
                    "date_time": date_time,
                    "speakers": speakers,
                },
            }
        )
    return output


def extract_response_text(body: dict[str, Any]) -> str:
    try:
        for item in body.get("output", []):
            if item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        return content.get("text", "")
        for item in body.get("output", []):
            if "text" in item:
                return item["text"]
            for content in item.get("content", []):
                if "text" in content:
                    return content["text"]
    except Exception:
        pass
    return ""


def extract_response_model(body: dict[str, Any]) -> str:
    for key in ("model", "actual_model", "provider_model"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    result = body.get("result")
    if isinstance(result, dict):
        for key in ("model", "actual_model", "provider_model"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def normalize_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    usage = usage or {}
    input_tokens = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
    output_tokens = int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
    total_tokens = int(usage.get("total_tokens", input_tokens + output_tokens) or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def send_message(
    *,
    base_url: str,
    token: str,
    user: str,
    message: str,
    timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/v1/responses"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    payload: dict[str, Any] = {
        "model": "openclaw",
        "input": message,
        "stream": False,
    }
    if user:
        payload["user"] = user
    resp = requests.post(url, json=payload, headers=headers, timeout=timeout_seconds)
    resp.raise_for_status()
    body = resp.json()
    return {
        "text": extract_response_text(body),
        "body": body,
        "usage": normalize_usage(body.get("usage") if isinstance(body, dict) else {}),
        "model": extract_response_model(body) if isinstance(body, dict) else "",
    }


def send_message_with_retry(
    *,
    base_url: str,
    token: str,
    user: str,
    message: str,
    timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    retries: int = 2,
) -> dict[str, Any]:
    overall_start = time.time()
    attempts: list[dict[str, Any]] = []
    retry_count = 0
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        attempt_start = time.time()
        try:
            result = send_message(
                base_url=base_url,
                token=token,
                user=user,
                message=message,
                timeout_seconds=timeout_seconds,
            )
            overall_end = time.time()
            result.update(
                {
                    "error": None,
                    "attempt_count": attempt + 1,
                    "retry_count": retry_count,
                    "request_start_ts": iso_from_epoch(overall_start),
                    "request_end_ts": iso_from_epoch(overall_end),
                    "elapsed_ms": elapsed_ms(overall_start, overall_end),
                    "attempts": attempts,
                }
            )
            return result
        except Exception as exc:
            last_exc = exc
            attempt_end = time.time()
            attempts.append(
                {
                    "attempt": attempt + 1,
                    "start_ts": iso_from_epoch(attempt_start),
                    "end_ts": iso_from_epoch(attempt_end),
                    "elapsed_ms": elapsed_ms(attempt_start, attempt_end),
                    "error": str(exc),
                }
            )
            if attempt < retries:
                retry_count += 1
                print(f"[eval] retry {attempt + 1}/{retries}: {exc}", file=sys.stderr)
                time.sleep(1.0 + attempt)
    overall_end = time.time()
    assert last_exc is not None
    return {
        "text": "",
        "body": {"error": str(last_exc)},
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "model": "",
        "error": str(last_exc),
        "attempt_count": retries + 1,
        "retry_count": retry_count,
        "request_start_ts": iso_from_epoch(overall_start),
        "request_end_ts": iso_from_epoch(overall_end),
        "elapsed_ms": elapsed_ms(overall_start, overall_end),
        "attempts": attempts,
    }


def sessions_file(openclaw_home: Path) -> Path:
    return openclaw_home / "agents" / "main" / "sessions" / "sessions.json"


def sessions_dir(openclaw_home: Path) -> Path:
    return openclaw_home / "agents" / "main" / "sessions"


def get_session_record(openclaw_home: Path, user: str) -> dict[str, Any] | None:
    path = sessions_file(openclaw_home)
    if not path.exists():
        return None
    data = read_json(path, default={})
    if not isinstance(data, dict):
        return None

    def normalize(value: dict[str, Any], key: str | None = None) -> dict[str, Any] | None:
        session_id = value.get("sessionId")
        if not isinstance(session_id, str) or not session_id.strip():
            return None
        session_key = value.get("sessionKey")
        user_value = value.get("user")
        return {
            "session_id": session_id.strip(),
            "session_key": session_key.strip() if isinstance(session_key, str) and session_key.strip() else None,
            "user": user_value.strip() if isinstance(user_value, str) and user_value.strip() else None,
            "lookup_key": key or None,
            "raw": value,
        }

    candidate_keys = [
        f"agent:main:openresponses-user:{user}",
        user,
        f"openresponses-user:{user}",
        f"agent:main:{user}",
    ]
    for key in candidate_keys:
        value = data.get(key)
        if isinstance(value, dict):
            normalized = normalize(value, key)
            if normalized is not None:
                return normalized

    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        normalized = normalize(value, key)
        if normalized is None:
            continue
        session_key = normalized.get("session_key")
        user_value = normalized.get("user")
        if isinstance(session_key, str) and session_key == user:
            return normalized
        if isinstance(user_value, str) and user_value == user:
            return normalized
        if isinstance(key, str) and key.endswith(f":{user}"):
            return normalized
    return None


def get_session_id(openclaw_home: Path, user: str) -> str | None:
    record = get_session_record(openclaw_home, user)
    if not isinstance(record, dict):
        return None
    session_id = record.get("session_id")
    return str(session_id).strip() or None


def canonical_openresponses_session_key(user: str) -> str:
    return f"agent:main:openresponses-user:{user}"


def _best_session_key(record: dict[str, Any] | None, user: str) -> str | None:
    if isinstance(record, dict):
        for key in ("session_key", "lookup_key"):
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return canonical_openresponses_session_key(user) if user else None


def _gateway_timeout_ms(timeout_seconds: float) -> int:
    try:
        seconds = float(timeout_seconds)
    except Exception:
        seconds = DEFAULT_RESET_TIMEOUT_SECONDS
    if seconds <= 0:
        seconds = DEFAULT_RESET_TIMEOUT_SECONDS
    return max(1000, int(round(seconds * 1000.0)))


def _run_openclaw_gateway_call(
    *,
    openclaw_bin: Path,
    cli_env: dict[str, str],
    method: str,
    params: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    timeout_ms = _gateway_timeout_ms(timeout_seconds)
    cmd = [
        str(openclaw_bin),
        "gateway",
        "call",
        method,
        "--json",
        "--timeout",
        str(timeout_ms),
        "--params",
        json.dumps(params, ensure_ascii=False),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=cli_env,
        timeout=max(float(timeout_seconds) + 5.0, 15.0),
    )
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    body: Any
    try:
        body = json.loads(stdout) if stdout else {}
    except Exception:
        body = {"raw_stdout": stdout}
    if proc.returncode != 0:
        raise RuntimeError(
            f"openclaw gateway call {method} failed with exit={proc.returncode}: "
            f"stdout={stdout[:1200]!r} stderr={stderr[:1200]!r}"
        )
    if isinstance(body, dict) and body.get("ok") is False:
        raise RuntimeError(f"openclaw gateway call {method} returned ok=false: {body!r}")
    return {
        "argv": cmd,
        "timeout_ms": timeout_ms,
        "stdout": stdout,
        "stderr": stderr,
        "body": body,
    }


def _wait_for_session_rotation(
    *,
    openclaw_home: Path,
    user: str,
    previous_session_id: str | None,
    timeout_seconds: float = DEFAULT_RESET_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    deadline = time.time() + timeout_seconds
    last_record: dict[str, Any] | None = None
    while time.time() < deadline:
        record = get_session_record(openclaw_home, user)
        if isinstance(record, dict):
            last_record = record
            current_id = str(record.get("session_id", "") or "").strip()
            if previous_session_id:
                if current_id and current_id != previous_session_id:
                    return record
            elif current_id:
                return record
        time.sleep(0.2)
    return last_record


def reset_session(
    *,
    openclaw_home: Path,
    user: str,
    session_id: str | None,
    session_key: str | None,
    reset_cli_bin: Path | None = None,
    reset_cli_env: dict[str, str] | None = None,
    timeout_seconds: float = DEFAULT_RESET_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    current_record = get_session_record(openclaw_home, user)
    key = (session_key or _best_session_key(current_record, user) or "").strip()
    previous_session_id = str(session_id or "").strip() or None
    if not key and not previous_session_id:
        return None
    if reset_cli_bin is None or reset_cli_env is None:
        raise RuntimeError(
            "Formal benchmark reset now requires a real Gateway sessions.reset call; "
            "reset_cli_bin/reset_cli_env were not provided."
        )

    rpc = _run_openclaw_gateway_call(
        openclaw_bin=reset_cli_bin,
        cli_env=reset_cli_env,
        method="sessions.reset",
        params={"key": key, "reason": "new"},
        timeout_seconds=timeout_seconds,
    )
    rotated_record = _wait_for_session_rotation(
        openclaw_home=openclaw_home,
        user=user,
        previous_session_id=previous_session_id,
        timeout_seconds=timeout_seconds,
    )
    next_session_id = None
    next_session_key = None
    if isinstance(rotated_record, dict):
        next_session_id = str(rotated_record.get("session_id", "") or "").strip() or None
        next_session_key = str(rotated_record.get("session_key", "") or "").strip() or None
    if previous_session_id and next_session_id == previous_session_id:
        raise RuntimeError(
            f"sessions.reset did not rotate session id for key={key}: old={previous_session_id} new={next_session_id}"
        )
    return {
        "method": "gateway.sessions.reset",
        "request": {"key": key, "reason": "new"},
        "rpc": rpc,
        "previous_session_id": previous_session_id,
        "next_session_id": next_session_id,
        "next_session_key": next_session_key,
    }


def parse_session_range(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    if "-" in value:
        lo, hi = value.split("-", 1)
        return int(lo), int(hi)
    n = int(value)
    return n, n


def _ov_usage_totals(snapshot: dict[str, Any] | None) -> dict[str, int] | None:
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


def _ov_usage_delta(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, int] | None:
    before_totals = _ov_usage_totals(before)
    after_totals = _ov_usage_totals(after)
    if before_totals is None or after_totals is None:
        return None
    return {
        "input_tokens": max(0, after_totals["input_tokens"] - before_totals["input_tokens"]),
        "output_tokens": max(0, after_totals["output_tokens"] - before_totals["output_tokens"]),
        "total_tokens": max(0, after_totals["total_tokens"] - before_totals["total_tokens"]),
    }


def _sum_usage_dicts(rows: list[dict[str, Any]], key: str) -> dict[str, int] | None:
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    have_any = False
    for row in rows:
        value = row.get(key)
        if not isinstance(value, dict):
            continue
        have_any = True
        for item in totals:
            totals[item] += int(value.get(item, 0) or 0)
    return totals if have_any else None


def ingest_sample(
    *,
    dataset_path: Path,
    sample_index: int,
    user: str,
    tail: str,
    base_url: str,
    token: str,
    openclaw_home: Path,
    output_json: Path,
    sessions_value: str | None = None,
    reset_cli_bin: Path | None = None,
    reset_cli_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    sample = load_locomo_data(dataset_path, sample_index)[0]
    sample_id = canonical_sample_id(sample, sample_index)
    session_range = parse_session_range(sessions_value)
    session_messages = build_session_messages(sample, tail=tail, session_range=session_range)
    records: list[dict[str, Any]] = []
    usage_total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    stage_start = time.time()

    for idx, bundle in enumerate(session_messages, start=1):
        response = send_message_with_retry(
            base_url=base_url,
            token=token,
            user=user,
            message=bundle["message"],
        )
        if response.get("error"):
            raise RuntimeError(f"Ingest failed for {sample_id} turn {idx}: {response['error']}")
        for key in usage_total:
            usage_total[key] += int(response["usage"].get(key, 0) or 0)
        session_record = get_session_record(openclaw_home, user)
        current_session_id = session_record.get("session_id") if isinstance(session_record, dict) else None
        current_session_key = session_record.get("session_key") if isinstance(session_record, dict) else None
        session_lookup_key = session_record.get("lookup_key") if isinstance(session_record, dict) else None
        reset_result = reset_session(
            openclaw_home=openclaw_home,
            user=user,
            session_id=current_session_id,
            session_key=current_session_key or session_lookup_key,
            reset_cli_bin=reset_cli_bin,
            reset_cli_env=reset_cli_env,
        ) if (current_session_id or current_session_key or session_lookup_key) else None
        records.append(
            {
                "sample_id": sample_id,
                "sample_index": sample_index,
                "user": user,
                "turn_index": idx,
                "session_key": bundle["meta"]["session_key"],
                "date_time": bundle["meta"]["date_time"],
                "message": bundle["message"],
                "reply": response["text"],
                "usage": response["usage"],
                "gateway_model_id": response.get("model", ""),
                "gateway_response": response["body"],
                "session_id": current_session_id,
                "runtime_session_key": current_session_key,
                "runtime_session_lookup_key": session_lookup_key,
                "archived_session_file": None,
                "reset": reset_result,
                "request_start_ts": response["request_start_ts"],
                "request_end_ts": response["request_end_ts"],
                "request_elapsed_ms": response["elapsed_ms"],
                "retry_count": response["retry_count"],
                "attempt_count": response["attempt_count"],
                "attempts": response["attempts"],
                "ts": response["request_end_ts"],
            }
        )

    stage_end = time.time()
    result = {
        "sample_id": sample_id,
        "sample_index": sample_index,
        "user": user,
        "session_count": len(records),
        "ingest_start_ts": iso_from_epoch(stage_start),
        "ingest_end_ts_gateway_only": iso_from_epoch(stage_end),
        "ingest_elapsed_ms_gateway_only": elapsed_ms(stage_start, stage_end),
        "usage_total": usage_total,
        "results": records,
    }
    write_json(output_json, result)
    return result


async def qa_sample_async(
    *,
    dataset_path: Path,
    sample_index: int,
    user: str,
    base_url: str,
    token: str,
    openclaw_home: Path,
    output_jsonl: Path,
    ov_client: Any | None = None,
    retries: int = 2,
    reset_cli_bin: Path | None = None,
    reset_cli_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    sample = load_locomo_data(dataset_path, sample_index)[0]
    sample_id = canonical_sample_id(sample, sample_index)
    qas = iter_formal_qas(sample, sample_index=sample_index)
    ensure_dir(output_jsonl.parent)
    usage_total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    records: list[dict[str, Any]] = []

    for qi, qa in enumerate(qas, start=1):
        ov_before = capture_vlm_snapshot(ov_client) if ov_client is not None else None
        response = await asyncio.to_thread(
            send_message_with_retry,
            base_url=base_url,
            token=token,
            user=user,
            message=qa["question"],
            retries=retries,
        )
        ov_after = capture_vlm_snapshot(ov_client) if ov_client is not None else None

        for key in usage_total:
            usage_total[key] += int(response["usage"].get(key, 0) or 0)

        session_record = get_session_record(openclaw_home, user)
        current_session_id = session_record.get("session_id") if isinstance(session_record, dict) else None
        current_session_key = session_record.get("session_key") if isinstance(session_record, dict) else None
        session_lookup_key = session_record.get("lookup_key") if isinstance(session_record, dict) else None
        reset_result = reset_session(
            openclaw_home=openclaw_home,
            user=user,
            session_id=current_session_id,
            session_key=current_session_key or session_lookup_key,
            reset_cli_bin=reset_cli_bin,
            reset_cli_env=reset_cli_env,
        ) if (current_session_id or current_session_key or session_lookup_key) else None
        ov_delta = _ov_usage_delta(ov_before, ov_after)
        record = {
            "sample_id": sample_id,
            "sample_index": sample_index,
            "user": user,
            "qi": qi,
            "qa_index": qa["qa_index"],
            "case_uid": qa["case_uid"],
            "question": qa["question"],
            "gold_answer": qa["gold_answer"],
            "expected": qa["gold_answer"],
            "prediction": response["text"],
            "response": response["text"],
            "category": qa["category"],
            "evidence": qa.get("evidence", []),
            "usage": response["usage"],
            "gateway_model_id": response.get("model", ""),
            "gateway_response": response["body"],
            "session_id": current_session_id,
            "runtime_session_key": current_session_key,
            "runtime_session_lookup_key": session_lookup_key,
            "archived_session_file": None,
            "reset": reset_result,
            "error": response.get("error"),
            "qa_error_flag": bool(response.get("error")),
            "qa_start_ts": response["request_start_ts"],
            "qa_end_ts": response["request_end_ts"],
            "qa_elapsed_ms": response["elapsed_ms"],
            "qa_retry_count": response["retry_count"],
            "qa_attempt_count": response["attempt_count"],
            "qa_attempts": response["attempts"],
            "ov_usage_before": ov_before,
            "ov_usage_after": ov_after,
            "ov_usage_delta": ov_delta,
            "ts": response["request_end_ts"],
        }
        append_jsonl(output_jsonl, record)
        records.append(record)

    summary = {
        "sample_id": sample_id,
        "sample_index": sample_index,
        "user": user,
        "qa_count": len(records),
        "usage_total": usage_total,
        "ov_usage_total": _sum_usage_dicts(records, "ov_usage_delta"),
        "records": records,
    }
    write_json(output_jsonl.with_suffix(output_jsonl.suffix + ".summary.json"), summary)
    return summary


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Experiment evaluation harness for OpenClaw + OpenViking.")
    sub = parser.add_subparsers(dest="mode", required=True)

    validate = sub.add_parser("validate", help="Validate dataset counts.")
    validate.add_argument("dataset", type=Path)

    ingest = sub.add_parser("ingest", help="Ingest one LoCoMo sample through OpenClaw Gateway.")
    ingest.add_argument("dataset", type=Path)
    ingest.add_argument("--sample", type=int, required=True)
    ingest.add_argument("--user", required=True)
    ingest.add_argument("--tail", default="[remember what's said, keep existing memory]")
    ingest.add_argument("--base-url", required=True)
    ingest.add_argument("--token", required=True)
    ingest.add_argument("--openclaw-home", dest="openclaw_home", type=Path)
    ingest.add_argument("--workdir", dest="openclaw_home", type=Path)
    ingest.add_argument("--output", type=Path, required=True)
    ingest.add_argument("--sessions", default=None)

    qa = sub.add_parser("qa", help="Run QA for one LoCoMo sample.")
    qa.add_argument("dataset", type=Path)
    qa.add_argument("--sample", type=int, required=True)
    qa.add_argument("--user", required=True)
    qa.add_argument("--base-url", required=True)
    qa.add_argument("--token", required=True)
    qa.add_argument("--openclaw-home", dest="openclaw_home", type=Path)
    qa.add_argument("--workdir", dest="openclaw_home", type=Path)
    qa.add_argument("--output", type=Path, required=True)
    return parser


def _require_openclaw_home(value: Path | None) -> Path:
    if value is None:
        raise RuntimeError("Either --openclaw-home or --workdir is required.")
    return value


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.mode == "validate":
        result = validate_dataset(args.dataset)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result.get("valid_formal_dataset") else 1

    if args.mode == "ingest":
        result = ingest_sample(
            dataset_path=args.dataset,
            sample_index=args.sample,
            user=args.user,
            tail=args.tail,
            base_url=args.base_url,
            token=args.token,
            openclaw_home=_require_openclaw_home(args.openclaw_home),
            output_json=args.output,
            sessions_value=args.sessions,
        )
        print(
            json.dumps(
                {
                    "sample_id": result["sample_id"],
                    "usage_total": result["usage_total"],
                    "count": len(result["results"]),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    if args.mode == "qa":
        summary = asyncio.run(
            qa_sample_async(
                dataset_path=args.dataset,
                sample_index=args.sample,
                user=args.user,
                base_url=args.base_url,
                token=args.token,
                openclaw_home=_require_openclaw_home(args.openclaw_home),
                output_jsonl=args.output,
            )
        )
        print(
            json.dumps(
                {
                    "sample_id": summary["sample_id"],
                    "qa_count": summary["qa_count"],
                    "usage_total": summary["usage_total"],
                    "ov_usage_total": summary.get("ov_usage_total"),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    parser.error(f"Unknown mode: {args.mode}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
