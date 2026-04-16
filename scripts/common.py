from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
import random
import re
import shutil
import string
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable

import requests


def utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def utc_now_iso_ms() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_from_epoch(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def elapsed_ms(start_ts: float, end_ts: float) -> int:
    return max(0, int(round((end_ts - start_ts) * 1000.0)))


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path, default: Any | None = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, data: Any) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def write_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    path.write_text(text, encoding="utf-8")


def _seed_benchmark_workspace(path: Path) -> None:
    ensure_dir(path)
    ensure_dir(path / "memory")
    files = {
        "AGENTS.md": "# AGENTS.md\n\nThis workspace is used by a benchmark harness. Follow the current user request directly. Do not run onboarding or self-introduction rituals. During benchmark ingest and QA, do not create, edit, read, or maintain memory/note/log files unless the user explicitly asks for a file operation. Rely on the configured OpenClaw/OpenViking memory pipeline instead of manual note-taking.\n",
        "SOUL.md": "# SOUL.md\n\nNeutral, concise, task-focused.\n",
        "IDENTITY.md": "# IDENTITY.md\n\n- Name: benchmark-assistant\n- Creature: assistant\n- Vibe: neutral and concise\n- Emoji: OK\n",
        "USER.md": "# USER.md\n\n- Notes: benchmark user\n",
        "TOOLS.md": "# TOOLS.md\n\nUse tools only when they are genuinely needed for the user's current request. Never use tools just to \"remember\", summarize, or persist conversation content.\n",
        "HEARTBEAT.md": "# HEARTBEAT.md\n\nBenchmark workspace: keep heartbeat empty.\n",
        "MEMORY.md": "# MEMORY.md\n\nBenchmark workspace. No manual long-term notes are allowed here during formal runs.\n",
    }
    for rel, content in files.items():
        write_text(path / rel, content)
    bootstrap = path / "BOOTSTRAP.md"
    if bootstrap.exists():
        bootstrap.unlink()


def prepare_default_benchmark_workspaces(state_dir: Path, agent_id: str = "main") -> list[Path]:
    candidates: list[Path] = []
    raw_candidates = [
        state_dir / "workspace",
        state_dir / f"workspace-{agent_id}",
        state_dir / "agents" / agent_id / "workspace",
    ]
    seen: set[str] = set()
    for candidate in raw_candidates:
        key = str(candidate.resolve()) if candidate.exists() else str(candidate)
        if key in seen:
            continue
        seen.add(key)
        _seed_benchmark_workspace(candidate)
        candidates.append(candidate)
    return candidates


def append_jsonl(path: Path, item: Any) -> None:
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False) + "\n")


def run_cmd(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    capture: bool = False,
    stdout_handle: Any | None = None,
    stderr_handle: Any | None = None,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {
        "cwd": str(cwd) if cwd else None,
        "env": env,
        "text": True,
    }
    if stdout_handle is not None:
        kwargs["stdout"] = stdout_handle
    elif capture:
        kwargs["stdout"] = subprocess.PIPE
    if stderr_handle is not None:
        kwargs["stderr"] = stderr_handle
    elif capture:
        kwargs["stderr"] = subprocess.PIPE
    proc = subprocess.run(cmd, **kwargs)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"Command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout or ''}\n"
            f"stderr:\n{proc.stderr or ''}"
        )
    return proc


def start_process(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    stdout_handle: Any | None = None,
    stderr_handle: Any | None = None,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        stdout=stdout_handle if stdout_handle is not None else subprocess.PIPE,
        stderr=stderr_handle if stderr_handle is not None else subprocess.PIPE,
    )


def which_or_raise(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"Required command not found on PATH: {name}")
    return path


def load_lock_file(root: Path) -> dict[str, Any]:
    return read_json(root / "VERSIONS.lock.json", default={}) or {}


def random_token(n_bytes: int = 32) -> str:
    alphabet = string.hexdigits.lower()
    return "".join(random.choice(alphabet[:16]) for _ in range(n_bytes * 2))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def copytree_clean(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def remove_if_exists(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def git_clone_or_update(url: str, ref: str, dest: Path, *, force_reclone: bool = False) -> str:
    if force_reclone and dest.exists():
        shutil.rmtree(dest)
    if not dest.exists():
        run_cmd(["git", "clone", "--depth", "1", "--branch", ref, url, str(dest)])
    else:
        try:
            run_cmd(["git", "-C", str(dest), "fetch", "--depth", "1", "origin", ref], check=True, capture=True)
            run_cmd(["git", "-C", str(dest), "checkout", ref], check=True, capture=True)
            run_cmd(["git", "-C", str(dest), "reset", "--hard", f"origin/{ref}"], check=True, capture=True)
        except Exception:
            # branch may be a tag
            run_cmd(["git", "-C", str(dest), "fetch", "--tags", "--force"], check=True, capture=True)
            run_cmd(["git", "-C", str(dest), "checkout", ref], check=True, capture=True)
    proc = run_cmd(["git", "-C", str(dest), "rev-parse", "HEAD"], capture=True)
    return (proc.stdout or "").strip()


def get_git_head(dest: Path) -> str:
    if not dest.exists():
        return ""
    try:
        proc = run_cmd(["git", "-C", str(dest), "rev-parse", "HEAD"], capture=True)
    except Exception:
        return ""
    return (proc.stdout or "").strip()


def nested_set(data: dict[str, Any], dotted_key: str, value: Any) -> None:
    parts = dotted_key.split(".")
    current: dict[str, Any] = data
    for part in parts[:-1]:
        existing = current.get(part)
        if not isinstance(existing, dict):
            existing = {}
            current[part] = existing
        current = existing
    current[parts[-1]] = value


def json_redact(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            lower = key.lower()
            if any(s in lower for s in ("api_key", "token", "secret", "password")):
                out[key] = "<REDACTED>"
            else:
                out[key] = json_redact(item)
        return out
    if isinstance(value, list):
        return [json_redact(item) for item in value]
    if isinstance(value, str) and len(value) > 24 and re.fullmatch(r"[A-Za-z0-9_\-\.]+", value):
        # keep normal ids / filenames; redact only obviously secret-ish strings later
        return value
    return value


def redact_text_secrets(text: str, secrets: Iterable[str]) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "<REDACTED>")
    return redacted


def patch_openclaw_config(
    config: dict[str, Any],
    *,
    group: dict[str, Any],
    openviking_enabled: bool,
    openviking_config_path: str,
    openviking_port: int,
    agent_id: str,
    gateway_port: int,
    primary_model_ref: str | None,
    workspace_path: str | None = None,
) -> dict[str, Any]:
    patched = json.loads(json.dumps(config))
    nested_set(patched, "gateway.http.endpoints.responses.enabled", True)
    nested_set(patched, "gateway.http.endpoints.chatCompletions.enabled", True)
    nested_set(patched, "gateway.port", gateway_port)
    nested_set(patched, "plugins.slots.memory", group["plugins.slots.memory"])
    nested_set(patched, "plugins.slots.contextEngine", group["plugins.slots.contextEngine"])
    nested_set(patched, "plugins.entries.openviking.enabled", openviking_enabled)
    nested_set(patched, "plugins.deny", list(group["plugins.deny"]))
    nested_set(patched, "plugins.entries.openviking.config.mode", "local")
    nested_set(patched, "plugins.entries.openviking.config.configPath", openviking_config_path)
    nested_set(patched, "plugins.entries.openviking.config.port", openviking_port)
    nested_set(patched, "plugins.entries.openviking.config.agentId", agent_id)
    nested_set(patched, "plugins.entries.openviking.config.autoCapture", True)
    nested_set(patched, "plugins.entries.openviking.config.autoRecall", True)
    nested_set(patched, "plugins.entries.openviking.config.emitStandardDiagnostics", True)
    nested_set(patched, "plugins.entries.openviking.config.logFindRequests", True)
    nested_set(patched, "agents.defaults.skipBootstrap", True)
    nested_set(patched, "agents.defaults.startupContext.enabled", False)
    nested_set(patched, "agents.defaults.contextInjection", "continuation-skip")
    if workspace_path:
        nested_set(patched, "agents.defaults.workspace", workspace_path)
    # Force a custom OpenAI-compatible Ark provider instead of volcengine-plan.
    # This avoids Coding Plan alias / subscription routing mismatches and lets
    # OpenClaw call the exact Ark model that the experiment requires.
    models = patched.setdefault("models", {})
    if not isinstance(models, dict):
        models = {}
        patched["models"] = models
    models["mode"] = "merge"
    providers = models.setdefault("providers", {})
    if not isinstance(providers, dict):
        providers = {}
        models["providers"] = providers
    ark_model_id = "doubao-seed-2.0-code"
    if primary_model_ref and primary_model_ref.startswith("arkapi/") and "/" in primary_model_ref:
        ark_model_id = primary_model_ref.split("/", 1)[1]
    providers["arkapi"] = {
        "baseUrl": "https://ark.cn-beijing.volces.com/api/v3",
        "apiKey": "${VOLCANO_ENGINE_API_KEY}",
        "api": "openai-completions",
        "models": [
            {
                "id": ark_model_id,
                "name": ark_model_id,
            }
        ],
    }

    if primary_model_ref:
        nested_set(patched, "agents.defaults.model.primary", primary_model_ref)
    return patched


def build_ov_conf(
    *,
    workspace: Path,
    volc_api_key: str,
    vlm_model: str,
    embedding_model: str,
    port: int,
) -> dict[str, Any]:
    return {
        "server": {
            "host": "127.0.0.1",
            "port": port,
            "root_api_key": ""
        },
        "memory": {"version": "v2"},
        "storage": {
            "workspace": str(workspace)
        },
        "embedding": {
            "max_retries": 3,
            "dense": {
                "backend": "volcengine",
                "api_base": "https://ark.cn-beijing.volces.com/api/v3",
                "api_key": volc_api_key,
                "provider": "volcengine",
                "model": embedding_model,
                "dimension": 1024,
                "input": "multimodal",
            }
        },
        "vlm": {
            "backend": "volcengine",
            "api_base": "https://ark.cn-beijing.volces.com/api/v3",
            "api_key": volc_api_key,
            "provider": "volcengine",
            "temperature": 0.1,
            "max_retries": 3,
            "model": vlm_model,
        },
    }


def tail_text(path: Path, max_lines: int = 120) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return ""
    return "\n".join(lines[-max_lines:])

def write_env_file(path: Path, values: dict[str, str]) -> None:
    lines = [f"{key}={value}" for key, value in values.items()]
    write_text(path, "\n".join(lines) + "\n")


def wait_for_http_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout_seconds: float = 120.0,
    sleep_seconds: float = 1.0,
) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last_exc: Exception | None = None
    while time.time() < deadline:
        try:
            resp = requests.get(url, headers=headers or {}, timeout=5)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_exc = exc
            time.sleep(sleep_seconds)
    raise RuntimeError(f"Timed out waiting for {url}: {last_exc}")


def wait_for_gateway_health(base_url: str, token: str, timeout_seconds: float = 120.0) -> None:
    deadline = time.time() + timeout_seconds
    last_exc: Exception | None = None
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    while time.time() < deadline:
        try:
            resp = requests.get(f"{base_url}/health", headers=headers, timeout=5)
            if resp.status_code < 500:
                try:
                    body = resp.json()
                except Exception:
                    body = {}
                result = body.get("result", body)
                if isinstance(result, dict):
                    status = result.get("status")
                    if result.get("ok") is True or status in {"ok", "live", "healthy"}:
                        return
                if body.get("status") in {"ok", "live", "healthy"}:
                    return
            time.sleep(1)
        except Exception as exc:
            last_exc = exc
            time.sleep(1)
    raise RuntimeError(f"Gateway healthcheck timed out for {base_url}: {last_exc}")


def guess_latest_run_id(group: str, sample_id: str, rerun: int) -> str:
    safe_sample = re.sub(r"[^A-Za-z0-9_.-]", "-", sample_id)
    return f"{group}__{safe_sample}__r{rerun}"


def parse_status_table(status: str) -> dict[str, Any]:
    """
    Best-effort parser for status strings returned by observer endpoints.
    This is intentionally tolerant because OpenViking currently returns
    formatted tables as plain strings.
    """
    lines = [line.strip() for line in (status or "").splitlines() if line.strip()]
    result: dict[str, Any] = {"raw": status, "rows": []}
    if not lines:
        return result
    header: list[str] = []
    for idx, line in enumerate(lines):
        cleaned = line.strip().strip("|")
        parts = [part.strip() for part in re.split(r"\s*\|\s*|\t+|\s{2,}", cleaned) if part.strip()]
        if not parts:
            continue
        if idx == 0:
            header = parts
            result["header"] = header
            continue
        result["rows"].append(parts)
    return result


def _normalized_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def extract_token_totals_from_vlm_status(status: str) -> dict[str, Any]:
    parsed = parse_status_table(status)
    header = [_normalized_header(h) for h in parsed.get("header", [])]
    input_keys = {"input", "prompt", "prompt_tokens", "input_tokens"}
    output_keys = {"output", "completion", "completion_tokens", "output_tokens", "generated", "generated_tokens"}
    total_keys = {"total", "total_tokens"}
    rows_out: list[dict[str, Any]] = []
    totals = {"input_tokens_total": 0, "output_tokens_total": 0, "total_tokens_total": 0}
    if len(header) < 2:
        return {"parsed": parsed, "rows": rows_out, **totals}
    normalized_columns = header[1:]
    for row in parsed.get("rows", []):
        if not isinstance(row, list) or len(row) < 2:
            continue
        label = str(row[0]).strip()
        values = row[1:]
        if len(values) < len(normalized_columns):
            values = values + [""] * (len(normalized_columns) - len(values))
        row_map: dict[str, int] = {}
        for key, val in zip(normalized_columns, values):
            try:
                row_map[key] = int(str(val).replace(",", ""))
            except ValueError:
                continue
        input_value = sum(value for key, value in row_map.items() if key in input_keys)
        output_value = sum(value for key, value in row_map.items() if key in output_keys)
        total_value = sum(value for key, value in row_map.items() if key in total_keys)
        if total_value == 0 and (input_value or output_value):
            total_value = input_value + output_value
        rows_out.append(
            {
                "label": label,
                "columns": row_map,
                "input_tokens": input_value,
                "output_tokens": output_value,
                "total_tokens": total_value,
            }
        )
        totals["input_tokens_total"] += input_value
        totals["output_tokens_total"] += output_value
        totals["total_tokens_total"] += total_value
    return {"parsed": parsed, "rows": rows_out, **totals}


def extract_input_tokens_from_vlm_status(status: str) -> dict[str, Any]:
    parsed = extract_token_totals_from_vlm_status(status)
    return {
        "parsed": parsed.get("parsed", {}),
        "row_input_tokens": {row.get("label", "row"): int(row.get("input_tokens", 0) or 0) for row in parsed.get("rows", [])},
        "input_tokens_total": int(parsed.get("input_tokens_total", 0) or 0),
        "output_tokens_total": int(parsed.get("output_tokens_total", 0) or 0),
        "total_tokens_total": int(parsed.get("total_tokens_total", 0) or 0),
    }


def safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def python_version_string() -> str:
    return sys.version.split()[0]


def command_version(cmd: list[str]) -> str:
    try:
        proc = run_cmd(cmd, capture=True, check=True)
        return (proc.stdout or proc.stderr or "").strip().splitlines()[0]
    except Exception:
        return ""
