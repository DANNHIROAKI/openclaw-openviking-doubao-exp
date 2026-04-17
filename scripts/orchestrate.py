from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import time

import requests
from pathlib import Path
from typing import Any

from .common import (
    build_ov_conf,
    command_version,
    copytree_clean,
    prepare_default_benchmark_workspaces,
    elapsed_ms,
    ensure_dir,
    get_git_head,
    git_clone_or_update,
    guess_latest_run_id,
    iso_from_epoch,
    json_redact,
    load_lock_file,
    openclaw_session_to_ov_storage_id,
    patch_openclaw_config,
    python_version_string,
    random_token,
    read_json,
    redact_text_secrets,
    run_cmd,
    safe_unlink,
    start_process,
    tail_text,
    utc_now_iso_ms,
    wait_for_gateway_health,
    write_env_file,
    write_json,
)
from .eval_harness import (
    get_session_id,
    ingest_sample,
    qa_sample_async,
    reset_session,
    send_message_with_retry,
    validate_dataset,
)
from .experiment_spec import (
    GROUP_ORDER,
    GROUPS,
    deterministic_user_key,
    is_ov_group,
    rotated_group_order,
    short_group_id,
)
from .judge_harness import grade_answers
from .metrics import materialize_run_metrics
from .openviking_probe import (
    OpenVikingClient,
    capture_snapshot,
    find_session_by_marker,
    latest_real_session,
    list_real_sessions,
    wait_for_commit_visibility,
    wait_for_sessions_visibility,
)
from .summary import summarize


def nested_get(data: dict[str, Any], dotted_key: str) -> Any:
    current: Any = data
    for part in dotted_key.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


class ExperimentOrchestrator:
    def __init__(self, root: Path, args: argparse.Namespace) -> None:
        self.root = root
        self.args = args
        self.lock = load_lock_file(root)

        self.started_at = utc_now_iso_ms()
        self.volcano_api_key = os.environ.get("VOLCANO_ENGINE_API_KEY", "").strip()
        if not self.volcano_api_key:
            raise RuntimeError("VOLCANO_ENGINE_API_KEY is required.")

        self.gateway_token = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "").strip() or random_token()
        self.ark_llm_endpoint_id = os.environ.get("ARK_LLM_ENDPOINT_ID", "").strip()
        self.ark_embedding_endpoint_id = os.environ.get("ARK_EMBEDDING_ENDPOINT_ID", "").strip()
        self.ark_embedding_model = os.environ.get("ARK_EMBEDDING_MODEL", "").strip()
        self.primary_model_ref = (
            os.environ.get("OPENCLAW_PRIMARY_MODEL_REF", "").strip()
            or self.lock.get("experiment_defaults", {}).get("primary_model_ref", "")
        )
        if self.primary_model_ref in {
            "volcengine-plan/doubao-seed-code",
            "volcengine-plan/ark-code-latest",
            "doubao-seed-code",
            "seed-2.0-code",
            "volcengine-plan/seed-2.0-code",
            "ark-code-latest",
            "",
        }:
            self.primary_model_ref = "arkapi/doubao-seed-2.0-code"
        if self.ark_llm_endpoint_id:
            self.primary_model_ref = f"arkapi/{self.ark_llm_endpoint_id}"
        self.judge_base_url = os.environ.get("JUDGE_BASE_URL", "").strip() or "https://ark.cn-beijing.volces.com/api/v3"
        self.judge_api_key = os.environ.get("JUDGE_API_KEY", "").strip() or self.volcano_api_key
        self.judge_model = os.environ.get("JUDGE_MODEL", "").strip() or self.lock.get("experiment_defaults", {}).get("judge_model", "")
        if self.ark_llm_endpoint_id and not os.environ.get("JUDGE_MODEL", "").strip():
            self.judge_model = self.ark_llm_endpoint_id
        if self.ark_llm_endpoint_id:
            self.lock.setdefault("experiment_defaults", {})["ov_vlm_model"] = self.ark_llm_endpoint_id
        if self.ark_embedding_endpoint_id:
            self.ark_embedding_model = self.ark_embedding_endpoint_id
        if self.ark_embedding_model:
            self.lock.setdefault("experiment_defaults", {})["ov_embedding_model"] = self.ark_embedding_model

        self.workspace_root = Path(os.environ.get("EXP_WORKSPACE_ROOT") or (root / "workspace"))
        self.cache_root = root / "cache"
        self.repos_root = self.cache_root / "repos"
        self.tool_root = self.cache_root / "tooling"
        self.templates_root = root / "templates"
        self.runs_root = root / "runs"
        self.artifacts_root = root / "artifacts"
        self.logs_root = self.artifacts_root / "logs"
        self.raw_root = self.artifacts_root / "raw"
        self.configs_root = self.artifacts_root / "configs"
        self.manifests_root = self.artifacts_root / "manifests"
        self.metrics_root = self.artifacts_root / "metrics"
        self.smoke_root = self.artifacts_root / "smoke"

        self.openclaw_cli_prefix = Path(os.environ.get("OPENCLAW_CLI_PREFIX") or (self.tool_root / "openclaw-cli"))
        self.openviking_tool_venv = Path(os.environ.get("OPENVIKING_TOOL_VENV") or (self.tool_root / "openviking-venv"))

        self.bootstrap_repo_dir = self.repos_root / "openclaw-openviking-doubao"
        bundled_plugin = self.lock.get("bundled_plugin_snapshot", {}) if isinstance(self.lock.get("bundled_plugin_snapshot"), dict) else {}
        self.vendored_plugin_dir = root / bundled_plugin.get("path", "vendor/openclaw-openviking-doubao/plugin")
        self.vendored_plugin_metadata_path = root / bundled_plugin.get("metadata_path", "vendor/openclaw-openviking-doubao/SYNC_SOURCE.json")
        self.vendored_plugin_versions_lock_path = root / bundled_plugin.get("versions_lock_path", "vendor/openclaw-openviking-doubao/VERSIONS.lock")
        self.dataset_repo_dir = self.repos_root / "OpenViking-LoCoMo10"
        self.official_openviking_repo_dir = self.repos_root / "OpenViking-official"
        self.official_openclaw_repo_dir = self.repos_root / "OpenClaw-official"

        self.dataset_path = self.dataset_repo_dir / self.lock["dataset_repo"]["dataset_path"]
        self.base_state_dir = self.workspace_root / "base" / "openclaw-state"
        self.base_config_path = self.base_state_dir / "openclaw.json"

        for path in [
            self.workspace_root,
            self.cache_root,
            self.repos_root,
            self.tool_root,
            self.templates_root,
            self.runs_root,
            self.artifacts_root,
            self.logs_root,
            self.raw_root,
            self.configs_root,
            self.manifests_root,
            self.metrics_root,
            self.smoke_root,
        ]:
            ensure_dir(path)

        self.secrets_for_redaction = [self.volcano_api_key, self.gateway_token, self.judge_api_key]
        self.provider_model_ids_seen: set[str] = set()
        self.judge_provider_model_ids_seen: set[str] = set()

    def log(self, text: str) -> None:
        print(text, flush=True)

    def cli_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = os.environ.copy()
        env["PATH"] = f"{self.openclaw_cli_prefix / 'bin'}:{env.get('PATH', '')}"
        env["VOLCANO_ENGINE_API_KEY"] = self.volcano_api_key
        env["OPENCLAW_GATEWAY_TOKEN"] = self.gateway_token
        env["PYTHONUNBUFFERED"] = "1"
        if extra:
            env.update(extra)
        return env

    def validate_embedding_model_access(self) -> None:
        embedding_model = str(self.lock.get("experiment_defaults", {}).get("ov_embedding_model", "") or "").strip()
        if not embedding_model:
            raise RuntimeError("OpenViking embedding model is empty. Set ARK_EMBEDDING_MODEL or ARK_EMBEDDING_ENDPOINT_ID.")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.volcano_api_key}",
        }
        attempts: list[tuple[str, dict[str, Any], str]] = [
            (
                "https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal",
                {
                    "model": embedding_model,
                    "input": [
                        {"type": "text", "text": "openviking embedding probe"}
                    ],
                },
                "multimodal",
            ),
            (
                "https://ark.cn-beijing.volces.com/api/v3/embeddings",
                {
                    "model": embedding_model,
                    "input": ["openviking embedding probe"],
                },
                "text",
            ),
        ]

        failures: list[str] = []
        for url, payload, mode in attempts:
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=30)
                if resp.status_code >= 400:
                    failures.append(f"{mode} endpoint -> HTTP {resp.status_code}: {(resp.text or '')[:800]}")
                    continue
                body = resp.json()
            except Exception as exc:  # pragma: no cover - runtime network path
                failures.append(f"{mode} endpoint -> exception: {exc}")
                continue

            data = body.get("data") if isinstance(body, dict) else None
            ok = False
            if isinstance(data, dict) and isinstance(data.get("embedding"), list) and data.get("embedding"):
                ok = True
            elif isinstance(data, list) and data:
                first = data[0]
                ok = isinstance(first, dict) and isinstance(first.get("embedding"), list) and bool(first.get("embedding"))
            if ok:
                self.log(f"Embedding probe succeeded via {mode} endpoint for model {embedding_model}.")
                return
            failures.append(f"{mode} endpoint -> unexpected response: {body!r}")

        raise RuntimeError(
            "OpenViking embedding precheck failed. "
            f"Model={embedding_model}. "
            "This runner can now probe both multimodal and text embedding APIs, but neither succeeded. "
            "Confirm that the embedding model is activated and keep ARK_EMBEDDING_MODEL / ARK_EMBEDDING_ENDPOINT_ID pointed at a working model or endpoint. "
            f"Attempts: {' | '.join(failures)}"
        )


    def preflight(self) -> dict[str, Any]:
        required = ["bash", "git", "curl", "node", "npm", "python3"]
        missing = [cmd for cmd in required if shutil.which(cmd) is None]
        if sys.version_info < (3, 10):
            raise RuntimeError(f"Python >= 3.10 required, got {python_version_string()}")
        if missing:
            raise RuntimeError(f"Missing required commands: {', '.join(missing)}")
        report = {
            "python": python_version_string(),
            "node": command_version(["node", "--version"]),
            "npm": command_version(["npm", "--version"]),
            "git": command_version(["git", "--version"]),
            "curl": command_version(["curl", "--version"]),
            "platform": platform.platform(),
            "effective_env": {
                "EXP_SKIP_SMOKE": os.environ.get("EXP_SKIP_SMOKE", "0"),
                "EXP_SAMPLE_FILTER": os.environ.get("EXP_SAMPLE_FILTER", ""),
                "EXP_GROUP_FILTER": os.environ.get("EXP_GROUP_FILTER", ""),
                "EXP_RERUNS": os.environ.get("EXP_RERUNS", "1"),
                "EXP_GATEWAY_REQUEST_TIMEOUT_S": os.environ.get("EXP_GATEWAY_REQUEST_TIMEOUT_S", "300"),
                "EXP_RESET_TIMEOUT_S": os.environ.get("EXP_RESET_TIMEOUT_S", os.environ.get("EXP_GATEWAY_REQUEST_TIMEOUT_S", "300")),
                "EXP_RESUME": os.environ.get("EXP_RESUME", "1"),
                "OPENCLAW_PRIMARY_MODEL_REF": self.primary_model_ref,
                "ARK_LLM_ENDPOINT_ID": self.ark_llm_endpoint_id,
                "ARK_EMBEDDING_ENDPOINT_ID": self.ark_embedding_endpoint_id,
                "EXP_WORKSPACE_ROOT": str(self.workspace_root),
                "OPENCLAW_CLI_PREFIX": str(self.openclaw_cli_prefix),
                "OPENVIKING_TOOL_VENV": str(self.openviking_tool_venv),
            },
        }
        write_json(self.artifacts_root / "preflight.json", report)
        return report

    def _clone_with_ref_candidates(self, *, url: str, dest: Path, candidates: list[str], force: bool) -> tuple[str, str]:
        last_exc: Exception | None = None
        for ref in candidates:
            if not ref:
                continue
            try:
                sha = git_clone_or_update(url, ref, dest, force_reclone=force)
                return sha, ref
            except Exception as exc:
                last_exc = exc
        raise RuntimeError(f"Failed to clone {url} with refs {candidates}: {last_exc}")

    def fetch_repos(self) -> dict[str, Any]:
        self.log("Fetching pinned helper repos ...")
        force = os.environ.get("EXP_FORCE_RECLONE", "0").strip() == "1"
        vendored_plugin_available = self.vendored_plugin_dir.exists()

        bootstrap_error = ""
        bootstrap_sha = ""
        bootstrap_ref = self.lock["bootstrap_repo"]["ref"]
        try:
            bootstrap_sha = git_clone_or_update(
                self.lock["bootstrap_repo"]["url"],
                bootstrap_ref,
                self.bootstrap_repo_dir,
                force_reclone=force,
            )
        except Exception as exc:
            bootstrap_error = str(exc)
            if vendored_plugin_available:
                self.log("Bootstrap helper repo clone failed; continuing with bundled synced plugin snapshot.")
            else:
                raise

        dataset_sha = git_clone_or_update(
            self.lock["dataset_repo"]["url"],
            self.lock["dataset_repo"]["ref"],
            self.dataset_repo_dir,
            force_reclone=force,
        )
        openviking_sha = git_clone_or_update(
            self.lock["openviking_repo"]["url"],
            self.lock["openviking_repo"]["ref"],
            self.official_openviking_repo_dir,
            force_reclone=force,
        )

        openclaw_repo_cfg = self.lock.get("openclaw_repo")
        openclaw_repo_url = ""
        openclaw_ref_candidates: list[str] = []
        if isinstance(openclaw_repo_cfg, dict):
            openclaw_repo_url = str(openclaw_repo_cfg.get("url", "") or "")
            openclaw_ref_candidates = [str(item) for item in openclaw_repo_cfg.get("ref_candidates", []) if str(item)]
        if not openclaw_repo_url:
            openclaw_repo_url = str(self.lock.get("official_docs", {}).get("openclaw", "") or "")
        if not openclaw_ref_candidates:
            version = str(self.lock["openclaw"]["version"])
            openclaw_ref_candidates = [version, f"v{version}", "main"]
        openclaw_sha, openclaw_ref = self._clone_with_ref_candidates(
            url=openclaw_repo_url,
            dest=self.official_openclaw_repo_dir,
            candidates=openclaw_ref_candidates,
            force=force,
        )

        result: dict[str, Any] = {
            "bootstrap_repo_commit": bootstrap_sha,
            "bootstrap_repo_ref": bootstrap_ref,
            "dataset_repo_commit": dataset_sha,
            "dataset_repo_ref": self.lock["dataset_repo"]["ref"],
            "official_openviking_repo_commit": openviking_sha,
            "official_openviking_repo_ref": self.lock["openviking_repo"]["ref"],
            "official_openclaw_repo_commit": openclaw_sha,
            "official_openclaw_repo_ref": openclaw_ref,
            "openclaw_eval_commit": get_git_head(self.root),
        }
        if bootstrap_error:
            result["bootstrap_repo_error"] = bootstrap_error
        write_json(self.artifacts_root / "repo_commits.json", result)
        return result

    def normalize_safe_modes(self, root_path: Path) -> None:
        if not root_path.exists():
            return
        for path in sorted(root_path.rglob("*")):
            try:
                if path.is_dir():
                    path.chmod(0o755)
                else:
                    path.chmod(0o644)
            except Exception:
                continue

    def describe_plugin_source(self) -> dict[str, Any]:
        if self.vendored_plugin_dir.exists():
            source: dict[str, Any] = {
                "mode": "vendored",
                "path": str(self.vendored_plugin_dir.relative_to(self.root)),
            }
            if self.vendored_plugin_versions_lock_path.exists():
                source["versions_lock_path"] = str(self.vendored_plugin_versions_lock_path.relative_to(self.root))
            metadata = read_json(self.vendored_plugin_metadata_path, default={})
            if isinstance(metadata, dict):
                source.update(metadata)
            return source
        return {
            "mode": "bootstrap_repo",
            "path": "cache/repos/openclaw-openviking-doubao/plugin",
            "repo_url": self.lock["bootstrap_repo"]["url"],
            "repo_ref": self.lock["bootstrap_repo"]["ref"],
        }

    def install_openclaw_cli(self) -> str:
        target_version = str(self.lock["openclaw"]["version"])
        openclaw_bin = self.openclaw_cli_prefix / "bin" / "openclaw"
        current_version = ""
        if openclaw_bin.exists():
            try:
                proc = run_cmd([str(openclaw_bin), "--version"], capture=True)
                current_version = (proc.stdout or proc.stderr or "").strip().splitlines()[0]
            except Exception:
                current_version = ""
        if target_version not in current_version:
            self.log(f"Installing OpenClaw CLI {target_version} ...")
            ensure_dir(self.openclaw_cli_prefix)
            install_cmd = (
                "curl -fsSL --proto '=https' --tlsv1.2 "
                f"{self.lock['openclaw']['install_url']} | "
                f"bash -s -- --prefix '{self.openclaw_cli_prefix}' --version {target_version}"
            )
            run_cmd(["bash", "-lc", install_cmd], env=self.cli_env())
        proc = run_cmd([str(openclaw_bin), "--version"], capture=True)
        version = (proc.stdout or proc.stderr or "").strip().splitlines()[0]
        if target_version not in version:
            raise RuntimeError(f"OpenClaw runtime version mismatch: expected {target_version}, got {version}")
        return version

    def validate_openviking_runtime(self) -> tuple[bool, str]:
        server_bin = self.openviking_tool_venv / "bin" / "openviking-server"
        if not server_bin.exists():
            return False, f"missing server binary: {server_bin}"
        try:
            proc = run_cmd([str(server_bin), "--help"], capture=True, env=self.cli_env())
            output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
            return True, output
        except Exception as exc:
            return False, str(exc)

    def ensure_source_build_prereqs(self) -> None:
        missing: list[str] = []
        if shutil.which("cargo") is None:
            missing.append("cargo (Rust toolchain)")
        if shutil.which("go") is None:
            missing.append("go")
        if shutil.which("g++") is None and shutil.which("clang++") is None:
            missing.append("g++/clang++")
        if missing:
            raise RuntimeError(
                f"OpenViking {self.lock['openviking']['version']} PyPI wheel self-check failed and source fallback requires: "
                + ", ".join(missing)
            )

    def install_openviking_runtime(self) -> str:
        python_bin = self.openviking_tool_venv / "bin" / "python"
        if not python_bin.exists():
            self.log("Creating shared OpenViking runtime venv ...")
            run_cmd(["python3", "-m", "venv", str(self.openviking_tool_venv)])
        pip_bin = self.openviking_tool_venv / "bin" / "pip"
        run_cmd([str(pip_bin), "install", "-U", "pip", "setuptools", "wheel"], capture=True)

        install_mode = os.environ.get("EXP_OPENVIKING_INSTALL_MODE", "auto").strip().lower() or "auto"
        if install_mode not in {"auto", "wheel", "source"}:
            raise RuntimeError(f"Unsupported EXP_OPENVIKING_INSTALL_MODE: {install_mode}")

        if install_mode in {"auto", "wheel"}:
            self.log(f"Installing OpenViking runtime ({self.lock['openviking']['pypi_spec']}) ...")
            run_cmd([str(pip_bin), "install", "--force-reinstall", self.lock["openviking"]["pypi_spec"]], capture=True)
            ok, validation_detail = self.validate_openviking_runtime()
            if ok:
                proc = run_cmd(
                    [str(python_bin), "-c", "import openviking; print(getattr(openviking, '__version__', 'unknown'))"],
                    capture=True,
                )
                version = (proc.stdout or "").strip()
                if self.lock["openviking"]["version"] not in version:
                    raise RuntimeError(f"OpenViking runtime version mismatch: expected {self.lock['openviking']['version']}, got {version}")
                return version
            if install_mode == "wheel":
                raise RuntimeError(
                    "OpenViking wheel installed but runtime self-check failed. "
                    f"Details: {validation_detail}"
                )
            self.log("OpenViking wheel self-check failed; attempting source fallback ...")
            run_cmd([str(pip_bin), "uninstall", "-y", "openviking"], capture=True, check=False)

        self.ensure_source_build_prereqs()
        source_path = self.official_openviking_repo_dir
        if not source_path.exists():
            raise RuntimeError(f"OpenViking source repo missing: {source_path}")
        self.log(f"Installing OpenViking from source ({source_path}) ...")
        run_cmd([str(pip_bin), "install", "--force-reinstall", str(source_path)], capture=True)
        ok, validation_detail = self.validate_openviking_runtime()
        if not ok:
            raise RuntimeError(
                "OpenViking source install completed but runtime self-check still failed. "
                f"Details: {validation_detail}"
            )
        proc = run_cmd(
            [str(python_bin), "-c", "import openviking; print(getattr(openviking, '__version__', 'unknown'))"],
            capture=True,
        )
        version = (proc.stdout or "").strip()
        if self.lock["openviking"]["version"] not in version:
            raise RuntimeError(f"OpenViking runtime version mismatch: expected {self.lock['openviking']['version']}, got {version}")
        return version

    def assert_locked_versions(self, openclaw_version: str, openviking_version: str) -> None:
        if str(self.lock["openclaw"]["version"]) not in openclaw_version:
            raise RuntimeError(f"OpenClaw version mismatch: {openclaw_version}")
        if str(self.lock["openviking"]["version"]) not in openviking_version:
            raise RuntimeError(f"OpenViking version mismatch: {openviking_version}")

    def base_state_env(self) -> dict[str, str]:
        return self.cli_env(
            {
                "OPENCLAW_STATE_DIR": str(self.base_state_dir),
                "OPENCLAW_CONFIG_PATH": str(self.base_config_path),
            }
        )

    def bootstrap_base_state(self) -> None:
        force = os.environ.get("EXP_FORCE_REBOOTSTRAP", "0").strip() == "1"
        bootstrap_marker = self.base_state_dir / ".bootstrap_complete.json"
        if bootstrap_marker.exists() and not force:
            prepare_default_benchmark_workspaces(self.base_state_dir)
            self.log("Reusing existing bootstrapped base OpenClaw state.")
            return
        if force and self.base_state_dir.exists():
            shutil.rmtree(self.base_state_dir)

        self.log("Bootstrapping base OpenClaw state ...")
        ensure_dir(self.base_state_dir)
        write_env_file(
            self.base_state_dir / ".env",
            {
                "VOLCANO_ENGINE_API_KEY": self.volcano_api_key,
                "OPENCLAW_GATEWAY_TOKEN": self.gateway_token,
            },
        )
        env = self.base_state_env()
        openclaw_bin = str(self.openclaw_cli_prefix / "bin" / "openclaw")

        if not self.base_config_path.exists():
            run_cmd(
                [
                    openclaw_bin,
                    "onboard",
                    "--non-interactive",
                    "--mode",
                    "local",
                    "--auth-choice",
                    "volcengine-api-key",
                    "--secret-input-mode",
                    "ref",
                    "--gateway-auth",
                    "token",
                    "--gateway-token-ref-env",
                    "OPENCLAW_GATEWAY_TOKEN",
                    "--skip-health",
                    "--accept-risk",
                ],
                env=env,
                capture=True,
            )

        plugin_dir = self.vendored_plugin_dir if self.vendored_plugin_dir.exists() else (self.bootstrap_repo_dir / "plugin")
        if not plugin_dir.exists():
            raise RuntimeError(
                "Plugin directory not found in bundled snapshot or bootstrap repo: "
                f"{plugin_dir}"
            )
        plugin_source = self.describe_plugin_source()
        self.log(f"Installing OpenViking OpenClaw plugin from {plugin_source.get('mode')} source: {plugin_dir}")
        run_cmd(
            [
                openclaw_bin,
                "plugins",
                "install",
                str(plugin_dir),
                "--force",
                "--dangerously-force-unsafe-install",
            ],
            env=env,
            capture=True,
        )
        try:
            run_cmd([openclaw_bin, "plugins", "enable", "openviking"], env=env, capture=True)
        except Exception:
            pass
        self.normalize_safe_modes(self.base_state_dir / "extensions" / "openviking")

        config = read_json(self.base_config_path, default={})
        if not isinstance(config, dict):
            raise RuntimeError(f"Base config is not JSON object: {self.base_config_path}")
        patched = patch_openclaw_config(
            config,
            group=GROUPS["g1-ov-nomemory"],
            openviking_enabled=True,
            openviking_config_path="/REPLACED/ov.conf",
            openviking_port=1933,
            agent_id="bootstrap-base",
            gateway_port=self.lock["experiment_defaults"]["gateway_port_base"],
            primary_model_ref=self.primary_model_ref,
            workspace_path=str(self.base_state_dir / "workspace"),
        )
        write_json(self.base_config_path, patched)
        prepare_default_benchmark_workspaces(self.base_state_dir)
        write_json(bootstrap_marker, {"completed_at": utc_now_iso_ms()})

    def clean_template_state(self, state_dir: Path) -> None:
        sessions_dir = state_dir / "agents" / "main" / "sessions"
        if sessions_dir.exists():
            for path in sessions_dir.glob("*.jsonl*"):
                path.unlink()
            safe_unlink(sessions_dir / "sessions.json")
        logs_dir = state_dir / "logs"
        if logs_dir.exists():
            shutil.rmtree(logs_dir)
        ensure_dir(sessions_dir)

    def build_templates(self) -> None:
        self.log("Building three locked group templates ...")
        base_config = read_json(self.base_config_path, default={})
        if not isinstance(base_config, dict):
            raise RuntimeError(f"Base config missing: {self.base_config_path}")

        for group_id, group in GROUPS.items():
            template_dir = self.templates_root / group_id
            template_state_dir = template_dir / "openclaw-state"
            template_ov_dir = template_dir / "openviking-workspace"
            if template_dir.exists():
                shutil.rmtree(template_dir)
            copytree_clean(self.base_state_dir, template_state_dir)
            ensure_dir(template_ov_dir)
            self.clean_template_state(template_state_dir)

            config_path = template_state_dir / "openclaw.json"
            template_config = read_json(config_path, default=copy.deepcopy(base_config))
            patched = patch_openclaw_config(
                template_config,
                group=group,
                openviking_enabled=group["plugins.entries.openviking.enabled"],
                openviking_config_path=str(template_ov_dir / "ov.conf"),
                openviking_port=self.lock["experiment_defaults"]["openviking_port_base"],
                agent_id=f"template-{group_id}",
                gateway_port=self.lock["experiment_defaults"]["gateway_port_base"],
                primary_model_ref=self.primary_model_ref,
                workspace_path=str(template_state_dir / "workspace"),
            )
            write_json(config_path, patched)
            prepare_default_benchmark_workspaces(template_state_dir)

            redacted_config = json_redact(patched)
            write_json(self.configs_root / f"group-{group_id}.openclaw.json", redacted_config)

            if is_ov_group(group_id):
                redacted_ov = build_ov_conf(
                    workspace=template_ov_dir,
                    volc_api_key="<REDACTED>",
                    vlm_model=self.lock["experiment_defaults"]["ov_vlm_model"],
                    embedding_model=self.lock["experiment_defaults"]["ov_embedding_model"],
                    port=self.lock["experiment_defaults"]["openviking_port_base"],
                )
                write_json(self.configs_root / f"group-{group_id}.ov.conf", redacted_ov)

    def run_env(self, run_state_dir: Path, run_config_path: Path, run_ov_conf_path: Path) -> dict[str, str]:
        return self.cli_env(
            {
                "HOME": str(run_state_dir.parent / ".runtime-home"),
                "OPENCLAW_HOME": str(run_state_dir.parent / ".runtime-home"),
                "OPENCLAW_STATE_DIR": str(run_state_dir),
                "OPENCLAW_CONFIG_PATH": str(run_config_path),
                "OPENVIKING_PYTHON": str(self.openviking_tool_venv / "bin" / "python"),
                "OPENVIKING_CONFIG_FILE": str(run_ov_conf_path),
            }
        )

    def spawn_gateway(self, *, run_env: dict[str, str], gateway_port: int, log_path: Path) -> subprocess.Popen[str]:
        openclaw_bin = str(self.openclaw_cli_prefix / "bin" / "openclaw")
        ensure_dir(log_path.parent)
        log_handle = log_path.open("w", encoding="utf-8")
        proc = start_process(
            [openclaw_bin, "gateway", "--port", str(gateway_port)],
            env=run_env,
            stdout_handle=log_handle,
            stderr_handle=log_handle,
        )
        proc._exp_log_handle = log_handle  # type: ignore[attr-defined]
        return proc

    def stop_gateway(self, proc: subprocess.Popen[str] | None) -> None:
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        try:
            handle = getattr(proc, "_exp_log_handle", None)
            if handle:
                handle.close()
        except Exception:
            pass

    def build_runtime_state(
        self,
        *,
        group_id: str,
        run_id: str,
        gateway_port: int,
        openviking_port: int,
    ) -> tuple[Path, Path, Path]:
        template_dir = self.templates_root / group_id
        if not template_dir.exists():
            raise RuntimeError(f"Template not found for {group_id}")
        run_dir = self.runs_root / run_id
        if run_dir.exists():
            shutil.rmtree(run_dir)
        run_state_dir = run_dir / "openclaw-state"
        run_ov_workspace = run_dir / "openviking-workspace"
        copytree_clean(template_dir / "openclaw-state", run_state_dir)
        ensure_dir(run_ov_workspace)
        self.clean_template_state(run_state_dir)
        self.normalize_safe_modes(run_state_dir / "extensions" / "openviking")

        run_config_path = run_state_dir / "openclaw.json"
        group = GROUPS[group_id]
        config = read_json(run_config_path, default={})
        patched = patch_openclaw_config(
            config,
            group=group,
            openviking_enabled=group["plugins.entries.openviking.enabled"],
            openviking_config_path=str(run_ov_workspace / "ov.conf"),
            openviking_port=openviking_port,
            agent_id=run_id,
            gateway_port=gateway_port,
            primary_model_ref=self.primary_model_ref,
            workspace_path=str(run_state_dir / "workspace"),
        )
        write_json(run_config_path, patched)
        prepare_default_benchmark_workspaces(run_state_dir)

        write_env_file(
            run_state_dir / ".env",
            {
                "VOLCANO_ENGINE_API_KEY": self.volcano_api_key,
                "OPENCLAW_GATEWAY_TOKEN": self.gateway_token,
            },
        )

        ov_conf = build_ov_conf(
            workspace=run_ov_workspace,
            volc_api_key=self.volcano_api_key,
            vlm_model=self.lock["experiment_defaults"]["ov_vlm_model"],
            embedding_model=self.lock["experiment_defaults"]["ov_embedding_model"],
            port=openviking_port,
        )
        run_ov_conf_path = run_ov_workspace / "ov.conf"
        write_json(run_ov_conf_path, ov_conf)

        write_json(self.configs_root / "runs" / f"{run_id}.openclaw.json", json_redact(patched))
        if is_ov_group(group_id):
            write_json(self.configs_root / "runs" / f"{run_id}.ov.conf", json_redact(ov_conf))

        return run_dir, run_state_dir, run_ov_conf_path

    def copy_openviking_logs(self, run_dir: Path, target_path: Path) -> None:
        ov_log_dir = run_dir / "openviking-workspace" / "data" / "log"
        ensure_dir(target_path.parent)
        if not ov_log_dir.exists():
            target_path.write_text("", encoding="utf-8")
            return
        parts: list[str] = []
        for path in sorted(ov_log_dir.glob("*.log")):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            parts.append(f"===== {path.name} =====\n{text}\n")
        target_path.write_text(redact_text_secrets("\n".join(parts), self.secrets_for_redaction), encoding="utf-8")

    def build_health_failure_detail(self, *, openclaw_log_path: Path, run_dir: Path) -> str:
        ov_log_dir = run_dir / "openviking-workspace" / "data" / "log"
        ov_tail_parts: list[str] = []
        if ov_log_dir.exists():
            for path in sorted(ov_log_dir.glob("*.log")):
                tail = tail_text(path, max_lines=120)
                if tail:
                    ov_tail_parts.append(f"===== {path.name} =====\n{tail}")
        openclaw_tail = tail_text(openclaw_log_path, max_lines=120)
        parts: list[str] = []
        if openclaw_tail:
            parts.append("[OpenClaw log tail]\n" + redact_text_secrets(openclaw_tail, self.secrets_for_redaction))
        if ov_tail_parts:
            parts.append("[OpenViking log tail]\n" + redact_text_secrets("\n\n".join(ov_tail_parts), self.secrets_for_redaction))
        return "\n\n".join(parts)

    def validate_group_runtime_config(self, config_path: Path, group_id: str) -> dict[str, Any]:
        config = read_json(config_path, default={})
        if not isinstance(config, dict):
            raise RuntimeError(f"Runtime config missing: {config_path}")
        expected = GROUPS[group_id]
        checks = {
            "memory": nested_get(config, "plugins.slots.memory") == expected["plugins.slots.memory"],
            "context_engine": nested_get(config, "plugins.slots.contextEngine") == expected["plugins.slots.contextEngine"],
            "openviking_enabled": nested_get(config, "plugins.entries.openviking.enabled") == expected["plugins.entries.openviking.enabled"],
            "plugins_deny": nested_get(config, "plugins.deny") == expected["plugins.deny"],
            "responses_enabled": nested_get(config, "gateway.http.endpoints.responses.enabled") is True,
        }
        if not all(checks.values()):
            raise RuntimeError(f"Config preflight failed for {group_id}: {json.dumps(checks, ensure_ascii=False)}")
        return checks

    def snapshot_session_ids(self, snapshot: dict[str, Any] | None) -> list[str]:
        if not isinstance(snapshot, dict):
            return []
        rows = snapshot.get("real_sessions")
        if not isinstance(rows, list):
            return []
        out: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            session_id = str(row.get("session_id", "") or "").strip()
            if session_id and session_id not in out:
                out.append(session_id)
        return out

    def resolve_ingest_ov_session_ids(
        self,
        *,
        ingest_result: dict[str, Any],
        ov_client: OpenVikingClient,
        pre_ingest_snapshot: dict[str, Any] | None,
    ) -> dict[str, Any]:
        mapped: list[dict[str, Any]] = []
        planned_ids: list[str] = []
        for record in ingest_result.get("results", []) if isinstance(ingest_result.get("results"), list) else []:
            if not isinstance(record, dict):
                continue
            session_id = str(record.get("session_id", "") or "").strip()
            runtime_session_key = str(record.get("runtime_session_key", "") or "").strip()
            if not session_id and not runtime_session_key:
                continue
            try:
                ov_session_id = openclaw_session_to_ov_storage_id(session_id or None, runtime_session_key or None)
                mapping_error = ""
            except Exception as exc:
                ov_session_id = ""
                mapping_error = str(exc)
            mapped.append(
                {
                    "openclaw_session_id": session_id or None,
                    "runtime_session_key": runtime_session_key or None,
                    "ov_session_id": ov_session_id or None,
                    "mapping_error": mapping_error or None,
                }
            )
            if ov_session_id and ov_session_id not in planned_ids:
                planned_ids.append(ov_session_id)

        before_ids = set(self.snapshot_session_ids(pre_ingest_snapshot))
        observed_items = list_real_sessions(ov_client)
        observed_ids: list[str] = []
        for item in observed_items:
            session_id = str(item.get("session_id", "") or "").strip()
            if session_id and session_id not in observed_ids:
                observed_ids.append(session_id)
        observed_new = [session_id for session_id in observed_ids if session_id not in before_ids]

        unexpected_observed_new = [session_id for session_id in observed_new if session_id not in planned_ids]

        target_ids: list[str] = []
        if planned_ids:
            for session_id in planned_ids:
                if session_id and session_id not in target_ids:
                    target_ids.append(session_id)
        else:
            for session_id in observed_new or observed_ids:
                if session_id and session_id not in target_ids:
                    target_ids.append(session_id)

        return {
            "mapped_from_ingest": mapped,
            "planned_session_ids": planned_ids,
            "observed_session_ids": observed_ids,
            "observed_new_session_ids": observed_new,
            "unexpected_observed_new_session_ids": unexpected_observed_new,
            "target_session_ids": target_ids,
        }

    def capture_run_manifest(
        self,
        *,
        run_id: str,
        group_id: str,
        sample_index: int,
        sample_id: str,
        rerun: int,
        user: str,
        start_iso: str,
        end_iso: str,
        runtime_versions: dict[str, str],
        ingest_stage: dict[str, Any],
        qa_summary: dict[str, Any],
        judge_summary: dict[str, Any],
        metrics_result: dict[str, Any],
        openclaw_log_path: Path,
        openviking_log_path: Path,
        observer_snapshot_path: Path | None,
    ) -> dict[str, Any]:
        direct_rows = metrics_result["task_direct_rows"]
        provider_models = sorted({str(row.get("gateway_model_id", "")) for row in direct_rows if str(row.get("gateway_model_id", "")).strip()})
        judge_provider_models = sorted({str(row.get("judge_provider_model_id", row.get("judge_model", ""))) for row in judge_summary.get("grades", []) if str(row.get("judge_provider_model_id", row.get("judge_model", ""))).strip()})
        formal_valid = bool(
            metrics_result["validation"]["all_pass"]
            and int(judge_summary.get("total", 0) or 0) == len(direct_rows)
            and (not is_ov_group(group_id) or metrics_result["sample_ingest_row"].get("formal_usage_complete"))
        )
        return {
            "run_id": run_id,
            "group_id": group_id,
            "group_short_id": short_group_id(group_id),
            "group_label": GROUPS[group_id]["label"],
            "rerun": rerun,
            "sample_index": sample_index,
            "sample_id": sample_id,
            "user": user,
            "start_time": start_iso,
            "end_time": end_iso,
            "success": True,
            "formal_valid": formal_valid,
            "runtime_versions": runtime_versions,
            "group_config": {
                "plugins.slots.memory": GROUPS[group_id]["plugins.slots.memory"],
                "plugins.slots.contextEngine": GROUPS[group_id]["plugins.slots.contextEngine"],
                "plugins.entries.openviking.enabled": GROUPS[group_id]["plugins.entries.openviking.enabled"],
                "plugins.deny": GROUPS[group_id]["plugins.deny"],
            },
            "correct": int(judge_summary.get("correct", 0) or 0),
            "total": int(judge_summary.get("total", 0) or 0),
            "score": judge_summary.get("score", 0.0),
            "category_breakdown": judge_summary.get("categories", {}),
            "judge_model": judge_summary.get("judge_model", self.judge_model),
            "judge_prompt_version": judge_summary.get("judge_prompt_version", ""),
            "judge_usage_total": judge_summary.get("judge_usage_total", {}),
            "provider_model_ids": provider_models,
            "judge_provider_model_ids": judge_provider_models,
            "ingest_stage": ingest_stage,
            "sample_ingest_metrics": metrics_result["sample_ingest_row"],
            "metrics_validation": metrics_result["validation"],
            "metrics_paths": metrics_result["paths"],
            "task_input_tokens_total": sum(int(row.get("task_input_tokens_amortized", 0) or 0) for row in metrics_result["task_amortized_rows"]),
            "task_elapsed_ms_total": sum(int(row.get("task_elapsed_ms_amortized", 0) or 0) for row in metrics_result["task_amortized_rows"]),
            "direct_qa_avg_elapsed_ms": (
                sum(int(row.get("qa_elapsed_ms", 0) or 0) for row in direct_rows) / len(direct_rows)
                if direct_rows
                else None
            ),
            "qa_usage_total": qa_summary.get("usage_total", {}),
            "openclaw_log": str(openclaw_log_path),
            "openviking_log": str(openviking_log_path),
            "observer_snapshot": str(observer_snapshot_path) if observer_snapshot_path else None,
        }

    def run_smoke_check(self) -> None:
        if os.environ.get("EXP_SKIP_SMOKE", "0").strip() == "1":
            self.log("Skipping OV smoke check due to EXP_SKIP_SMOKE=1")
            return

        self.log("Running OV smoke check ...")
        group_id = "g1-ov-nomemory"
        run_id = "smoke-g1-ov-nomemory"
        gateway_port = self.lock["experiment_defaults"]["gateway_port_base"] + 900
        openviking_port = self.lock["experiment_defaults"]["openviking_port_base"] + 900

        run_dir, run_state_dir, run_ov_conf_path = self.build_runtime_state(
            group_id=group_id,
            run_id=run_id,
            gateway_port=gateway_port,
            openviking_port=openviking_port,
        )
        log_path = self.smoke_root / "openclaw.log"
        env = self.run_env(run_state_dir, run_state_dir / "openclaw.json", run_ov_conf_path)
        proc: subprocess.Popen[str] | None = None
        try:
            proc = self.spawn_gateway(run_env=env, gateway_port=gateway_port, log_path=log_path)
            try:
                wait_for_gateway_health(f"http://127.0.0.1:{gateway_port}", self.gateway_token, timeout_seconds=180.0)
            except Exception as exc:
                detail = self.build_health_failure_detail(openclaw_log_path=log_path, run_dir=run_dir)
                raise RuntimeError(f"{exc}\n" + detail)
            client = OpenVikingClient(base_url=f"http://127.0.0.1:{openviking_port}", api_key="", agent_id=run_id)
            deadline = time.time() + 60
            while time.time() < deadline and not client.health():
                time.sleep(1)
            if not client.health():
                detail = self.build_health_failure_detail(openclaw_log_path=log_path, run_dir=run_dir)
                raise RuntimeError("OpenViking local service did not become healthy in smoke check.\n" + detail)

            probe = f"probe-{int(time.time())}-{random.randint(1000, 9999)}"
            prefix = (
                "[OPENVIKING-HEALTHCHECK] This is an automated healthcheck. "
                "All data below is synthetic and should be ignored after the check completes. "
                f"Probe marker: {probe}. "
            )
            user = f"ov-smoke-{int(time.time())}"
            msgs = [
                prefix
                + "Please remember the following SYNTHETIC test data: "
                "my name is Lin Zhou, I am rebuilding an order platform, "
                "my backend stack is Go, PostgreSQL, and Redis, and the current project progress is 70 percent. "
                "Reply briefly.",
                "[OPENVIKING-HEALTHCHECK] More synthetic test data for the same probe session. "
                "Our Kafka topic is order_events_v2, "
                "the payment callback service runs on payment-cb.internal:9443, "
                "and the main latency alert is P99 over 450ms for 3 minutes.",
                "[OPENVIKING-HEALTHCHECK] Additional synthetic test data. "
                "The inventory service exhausted its connection pool. "
                "We fixed it by raising max_open_conns from 80 to 160 and by adding a circuit breaker.",
                "[OPENVIKING-HEALTHCHECK] Synthetic preference for this test session only: "
                "keep answers concise, put the conclusion first, then the reason if needed.",
            ]
            for idx, msg in enumerate(msgs, start=1):
                result = send_message_with_retry(
                    base_url=f"http://127.0.0.1:{gateway_port}",
                    token=self.gateway_token,
                    user=user,
                    message=msg,
                )
                if result.get("error"):
                    raise RuntimeError(f"Smoke ingest failed at turn {idx}: {result['error']}")
                if idx < len(msgs):
                    time.sleep(1.0)

            time.sleep(4.0)
            smoke_session_id, _item, _context = find_session_by_marker(client, probe, session_scan_limit=12)
            if not smoke_session_id:
                smoke_session_id, _fallback = latest_real_session(client)
            if not smoke_session_id:
                raise RuntimeError("Smoke session not found in OpenViking after capture.")
            smoke_commit = client.commit_session(smoke_session_id, wait=False)
            smoke_commit_result = smoke_commit.get("result", smoke_commit) if isinstance(smoke_commit, dict) else {}
            smoke_commit_status = str(smoke_commit_result.get("status", "") if isinstance(smoke_commit_result, dict) else "").lower()
            if smoke_commit_status and smoke_commit_status not in {"accepted", "running", "completed", "ok", "success"}:
                raise RuntimeError(f"Smoke commit not accepted: {json.dumps(smoke_commit, ensure_ascii=False)}")
            try:
                client.wait_processed(timeout_seconds=60.0)
            except Exception:
                pass

            barrier = wait_for_commit_visibility(client=client, session_id=smoke_session_id, timeout_seconds=300.0)
            if not (barrier.get("commit_ok") and barrier.get("overview_ok") and barrier.get("memory_ok")):
                detail = self.build_health_failure_detail(openclaw_log_path=log_path, run_dir=run_dir)
                raise RuntimeError(f"Smoke barrier failed: {json.dumps(barrier, ensure_ascii=False)}\n" + detail)

            answer = send_message_with_retry(
                base_url=f"http://127.0.0.1:{gateway_port}",
                token=self.gateway_token,
                user=user,
                message=(
                    "[OPENVIKING-HEALTHCHECK] Based on the synthetic test data above, "
                    "summarize the backend stack and current project progress in one short sentence."
                ),
            )
            answer_text = answer.get("text", "")
            lowered = answer_text.lower()
            if not ("go" in lowered and "postgres" in lowered and "redis" in lowered and "70" in lowered):
                raise RuntimeError(f"Smoke same-session recall failed: {answer_text}")

            fresh = send_message_with_retry(
                base_url=f"http://127.0.0.1:{gateway_port}",
                token=self.gateway_token,
                user=f"{user}-fresh",
                message=(
                    "[OPENVIKING-HEALTHCHECK] Based on the synthetic test data from the healthcheck, "
                    "reply with the Kafka topic and payment callback service address in one line."
                ),
            )
            fresh_text = fresh.get("text", "")
            fresh_lower = fresh_text.lower()
            hits = sum(1 for kw in ("order_events_v2", "payment-cb.internal", "9443") if kw in fresh_lower)
            if hits < 2:
                raise RuntimeError(f"Smoke fresh-session recall failed: {fresh_text}")

            write_json(
                self.smoke_root / "smoke_result.json",
                {
                    "ok": True,
                    "probe": probe,
                    "answer": answer_text,
                    "fresh_answer": fresh_text,
                    "barrier": barrier,
                },
            )
        finally:
            self.stop_gateway(proc)
            self.copy_openviking_logs(run_dir, self.smoke_root / "openviking.log")
            if os.environ.get("EXP_KEEP_RUNS", "0").strip() != "1" and run_dir.exists():
                shutil.rmtree(run_dir)

    def maybe_skip_run(self, run_id: str) -> bool:
        resume = os.environ.get("EXP_RESUME", "1").strip() != "0"
        manifest_path = self.manifests_root / f"{run_id}.json"
        if not resume or not manifest_path.exists():
            return False
        data = read_json(manifest_path, default={})
        return isinstance(data, dict) and data.get("success") is True and data.get("formal_valid") is True

    def selected_groups(self) -> list[str]:
        filter_value = os.environ.get("EXP_GROUP_FILTER", "").strip()
        if not filter_value:
            return GROUP_ORDER[:]
        requested = [item.strip() for item in filter_value.split(",") if item.strip()]
        for item in requested:
            if item not in GROUPS:
                raise RuntimeError(f"Unknown group in EXP_GROUP_FILTER: {item}")
        return requested

    def selected_sample_indices(self, sample_count: int) -> list[int]:
        filter_value = os.environ.get("EXP_SAMPLE_FILTER", "").strip()
        if not filter_value:
            return list(range(sample_count))
        indices: list[int] = []
        for part in filter_value.split(","):
            part = part.strip()
            if not part:
                continue
            idx = int(part)
            if idx < 0 or idx >= sample_count:
                raise RuntimeError(f"Invalid sample index in EXP_SAMPLE_FILTER: {idx}")
            indices.append(idx)
        return indices

    def run_formal_experiment(self, repo_commits: dict[str, Any], runtime_versions: dict[str, str]) -> dict[str, Any]:
        dataset_validation = validate_dataset(self.dataset_path)
        if not dataset_validation.get("valid_formal_dataset"):
            raise RuntimeError(f"Dataset validation failed: {json.dumps(dataset_validation, ensure_ascii=False)}")
        write_json(self.artifacts_root / "dataset_validation.json", dataset_validation)

        dataset = read_json(self.dataset_path, default=[])
        if not isinstance(dataset, list):
            raise RuntimeError(f"Dataset missing or invalid: {self.dataset_path}")

        selected_groups = set(self.selected_groups())
        selected_indices = self.selected_sample_indices(len(dataset))
        reruns = int(os.environ.get("EXP_RERUNS", "1") or "1")
        quiet_wait_ms = int(os.environ.get("EXP_NOOV_QUIET_WAIT_MS", "3000") or "3000")

        run_counter = 0
        completed_run_ids: list[str] = []
        for rerun in range(1, reruns + 1):
            for sample_index in selected_indices:
                sample = dataset[sample_index]
                sample_id = str(sample.get("sample_id", "") or f"sample{sample_index + 1:02d}")
                order = [g for g in rotated_group_order(sample_index, rerun) if g in selected_groups]
                for group_id in order:
                    run_id = guess_latest_run_id(group_id, sample_id, rerun)
                    if self.maybe_skip_run(run_id):
                        self.log(f"Skipping completed run: {run_id}")
                        completed_run_ids.append(run_id)
                        continue

                    run_counter += 1
                    gateway_port = self.lock["experiment_defaults"]["gateway_port_base"] + run_counter
                    openviking_port = self.lock["experiment_defaults"]["openviking_port_base"] + run_counter
                    user = deterministic_user_key(group_id, rerun, sample_id)

                    start_iso = utc_now_iso_ms()
                    run_dir: Path | None = None
                    proc: subprocess.Popen[str] | None = None
                    ov_snapshots: dict[str, Any] = {}
                    observer_snapshot_path: Path | None = None
                    openclaw_log_path: Path | None = None
                    try:
                        self.log(f"Running {run_id} ...")
                        self.assert_locked_versions(runtime_versions["openclaw_version"], runtime_versions["openviking_version"])
                        run_dir, run_state_dir, run_ov_conf_path = self.build_runtime_state(
                            group_id=group_id,
                            run_id=run_id,
                            gateway_port=gateway_port,
                            openviking_port=openviking_port,
                        )
                        config_checks = self.validate_group_runtime_config(run_state_dir / "openclaw.json", group_id)
                        run_env = self.run_env(run_state_dir, run_state_dir / "openclaw.json", run_ov_conf_path)

                        openclaw_log_path = self.logs_root / "openclaw" / group_id / f"{sample_id}__r{rerun}.log"
                        proc = self.spawn_gateway(run_env=run_env, gateway_port=gateway_port, log_path=openclaw_log_path)
                        try:
                            wait_for_gateway_health(f"http://127.0.0.1:{gateway_port}", self.gateway_token, timeout_seconds=180.0)
                        except Exception as exc:
                            detail = self.build_health_failure_detail(openclaw_log_path=openclaw_log_path, run_dir=run_dir)
                            raise RuntimeError(f"{exc}\n" + detail)

                        ov_client: OpenVikingClient | None = None
                        if is_ov_group(group_id):
                            ov_client = OpenVikingClient(
                                base_url=f"http://127.0.0.1:{openviking_port}",
                                api_key="",
                                agent_id=run_id,
                            )
                            deadline = time.time() + 120
                            while time.time() < deadline and not ov_client.health():
                                time.sleep(1)
                            if not ov_client.health():
                                detail = self.build_health_failure_detail(openclaw_log_path=openclaw_log_path, run_dir=run_dir)
                                raise RuntimeError("OpenViking local service did not become healthy.\n" + detail)

                        ingest_start_epoch = time.time()
                        if ov_client is not None:
                            ov_snapshots["pre_ingest"] = capture_snapshot(ov_client)

                        ingest_output = self.raw_root / "ingest" / group_id / f"{sample_id}__r{rerun}.json"
                        ingest_result = ingest_sample(
                            dataset_path=self.dataset_path,
                            sample_index=sample_index,
                            user=user,
                            tail=self.lock.get("experiment_defaults", {}).get("tail", "[remember what's said, keep existing memory]"),
                            base_url=f"http://127.0.0.1:{gateway_port}",
                            token=self.gateway_token,
                            openclaw_home=run_state_dir,
                            output_json=ingest_output,
                            reset_cli_bin=self.openclaw_cli_prefix / "bin" / "openclaw",
                            reset_cli_env=run_env,
                        )

                        ov_barrier_wait_ms = 0
                        post_reset_quiet_wait_ms = 0
                        ov_target_session_count = 0
                        if ov_client is not None:
                            time.sleep(4.0)
                            ov_snapshots["post_ingest_precommit"] = capture_snapshot(ov_client)
                            session_resolution = self.resolve_ingest_ov_session_ids(
                                ingest_result=ingest_result,
                                ov_client=ov_client,
                                pre_ingest_snapshot=ov_snapshots.get("pre_ingest"),
                            )
                            planned_ids = session_resolution.get("planned_session_ids", []) if isinstance(session_resolution, dict) else []
                            if isinstance(planned_ids, list):
                                distinct_planned_ids = [sid for sid in planned_ids if isinstance(sid, str) and sid.strip()]
                            else:
                                distinct_planned_ids = []
                            expected_ingest_sessions = int(ingest_result.get("session_count", 0) or 0)
                            if expected_ingest_sessions > 0 and distinct_planned_ids and len(distinct_planned_ids) < expected_ingest_sessions:
                                detail = self.build_health_failure_detail(openclaw_log_path=openclaw_log_path, run_dir=run_dir)
                                raise RuntimeError(
                                    f"Gateway session reset ineffective for {run_id}: expected {expected_ingest_sessions} distinct ingest sessions, "
                                    f"but resolved only {len(distinct_planned_ids)} planned OpenViking session ids. "
                                    f"Resolution={json.dumps(session_resolution, ensure_ascii=False)}\n"
                                    + detail
                                )
                            target_session_ids = session_resolution.get("target_session_ids", []) if isinstance(session_resolution, dict) else []
                            ov_target_session_count = len(target_session_ids) if isinstance(target_session_ids, list) else 0
                            ov_snapshots["ingest_session_resolution"] = session_resolution
                            if not target_session_ids:
                                detail = self.build_health_failure_detail(openclaw_log_path=openclaw_log_path, run_dir=run_dir)
                                raise RuntimeError(
                                    f"No OpenViking sessions resolved after ingest for {run_id}: {json.dumps(session_resolution, ensure_ascii=False)}\n"
                                    + detail
                                )

                            commit_requests: list[dict[str, Any]] = []
                            commit_errors: list[dict[str, Any]] = []
                            for ingest_session_id in target_session_ids:
                                try:
                                    commit_response = ov_client.commit_session(ingest_session_id, wait=False)
                                    commit_result = commit_response.get("result", commit_response) if isinstance(commit_response, dict) else {}
                                    commit_status = str(commit_result.get("status", "") if isinstance(commit_result, dict) else "").lower()
                                    entry = {
                                        "session_id": ingest_session_id,
                                        "status": commit_status or None,
                                        "response": commit_response,
                                    }
                                    commit_requests.append(entry)
                                    if commit_status and commit_status not in {"accepted", "running", "completed", "ok", "success"}:
                                        commit_errors.append(entry)
                                except Exception as exc:
                                    entry = {
                                        "session_id": ingest_session_id,
                                        "error": str(exc),
                                    }
                                    commit_requests.append(entry)
                                    commit_errors.append(entry)
                            ov_snapshots["post_ingest_commit_requests"] = commit_requests
                            if commit_errors:
                                detail = self.build_health_failure_detail(openclaw_log_path=openclaw_log_path, run_dir=run_dir)
                                raise RuntimeError(
                                    f"OV commit not accepted for {run_id}: {json.dumps(commit_errors, ensure_ascii=False)}\n"
                                    + detail
                                )
                            try:
                                ov_snapshots["wait_processed"] = ov_client.wait_processed(
                                    timeout_seconds=max(60.0, 20.0 * float(max(1, ov_target_session_count)))
                                )
                            except Exception as exc:
                                ov_snapshots["wait_processed_error"] = str(exc)
                            barrier_start = time.time()
                            barrier = wait_for_sessions_visibility(
                                client=ov_client,
                                session_ids=target_session_ids,
                                timeout_seconds=300.0,
                                require_any_memory=True,
                                require_memory_for_each=False,
                            )
                            barrier_end = time.time()
                            ov_barrier_wait_ms = elapsed_ms(barrier_start, barrier_end)
                            ov_snapshots["post_ingest_barrier"] = barrier
                            barrier_ok = bool(
                                barrier.get("all_commit_ok")
                                and barrier.get("all_overview_ok")
                                and barrier.get("any_memory_ok")
                            )
                            if not barrier_ok:
                                detail = self.build_health_failure_detail(openclaw_log_path=openclaw_log_path, run_dir=run_dir)
                                raise RuntimeError(
                                    f"OV barrier failed for {run_id}: {json.dumps(barrier, ensure_ascii=False)}\n"
                                    + detail
                                )
                            ov_snapshots["post_ingest"] = capture_snapshot(ov_client)
                        else:
                            post_reset_quiet_wait_ms = quiet_wait_ms
                            time.sleep(quiet_wait_ms / 1000.0)
                        ingest_end_epoch = time.time()
                        ingest_stage = {
                            "ingest_start_ts": iso_from_epoch(ingest_start_epoch),
                            "ingest_end_ts": iso_from_epoch(ingest_end_epoch),
                            "ingest_elapsed_ms": elapsed_ms(ingest_start_epoch, ingest_end_epoch),
                            "ov_barrier_wait_ms": ov_barrier_wait_ms,
                            "ov_target_session_count": ov_target_session_count,
                            "post_reset_quiet_wait_ms": post_reset_quiet_wait_ms,
                            "config_preflight_checks": config_checks,
                            "formal_usage_complete": True,
                        }

                        qa_output = self.raw_root / "qa" / group_id / f"{sample_id}__r{rerun}.jsonl"
                        qa_summary = asyncio.run(
                            qa_sample_async(
                                dataset_path=self.dataset_path,
                                sample_index=sample_index,
                                user=user,
                                base_url=f"http://127.0.0.1:{gateway_port}",
                                token=self.gateway_token,
                                openclaw_home=run_state_dir,
                                output_jsonl=qa_output,
                                ov_client=ov_client if is_ov_group(group_id) else None,
                                reset_cli_bin=self.openclaw_cli_prefix / "bin" / "openclaw",
                                reset_cli_env=run_env,
                            )
                        )

                        if ov_client is not None:
                            ov_snapshots["post_run"] = capture_snapshot(ov_client)

                        if ov_snapshots:
                            observer_snapshot_path = self.raw_root / "observer" / group_id / f"{sample_id}__r{rerun}.json"
                            write_json(observer_snapshot_path, ov_snapshots)

                        judge_output = self.raw_root / "judge" / group_id / f"{sample_id}__r{rerun}.json"
                        judge_raw_output = self.raw_root / "judge_raw" / group_id / f"{sample_id}__r{rerun}.jsonl"
                        judge_summary = asyncio.run(
                            grade_answers(
                                answers_path=qa_output,
                                output_json=judge_output,
                                output_raw_jsonl=judge_raw_output,
                                base_url=self.judge_base_url,
                                api_key=self.judge_api_key,
                                model=self.judge_model,
                                concurrency=16,
                            )
                        )

                        metrics_result = materialize_run_metrics(
                            run_id=run_id,
                            group_id=group_id,
                            group_short_id=short_group_id(group_id),
                            group_label=GROUPS[group_id]["label"],
                            rerun_id=rerun,
                            sample_id=sample_id,
                            sample_index=sample_index,
                            ingest_result=ingest_result,
                            qa_summary=qa_summary,
                            judge_summary=judge_summary,
                            ingest_stage=ingest_stage,
                            ov_snapshots=ov_snapshots,
                            metrics_root=self.metrics_root,
                        )

                        if is_ov_group(group_id) and not metrics_result["sample_ingest_row"].get("formal_usage_complete"):
                            raise RuntimeError(f"OV internal usage incomplete for {run_id}")
                        if not metrics_result["validation"]["all_pass"]:
                            raise RuntimeError(f"Metrics reconciliation failed for {run_id}: {json.dumps(metrics_result['validation'], ensure_ascii=False)}")

                        for row in metrics_result["task_direct_rows"]:
                            model_id = str(row.get("gateway_model_id", "")).strip()
                            if model_id:
                                self.provider_model_ids_seen.add(model_id)
                        for row in judge_summary.get("grades", []):
                            model_id = str(row.get("judge_provider_model_id", row.get("judge_model", ""))).strip()
                            if model_id:
                                self.judge_provider_model_ids_seen.add(model_id)

                        end_iso = utc_now_iso_ms()
                        openviking_log_path = self.logs_root / "openviking" / group_id / f"{sample_id}__r{rerun}.log"
                        manifest = self.capture_run_manifest(
                            run_id=run_id,
                            group_id=group_id,
                            sample_index=sample_index,
                            sample_id=sample_id,
                            rerun=rerun,
                            user=user,
                            start_iso=start_iso,
                            end_iso=end_iso,
                            runtime_versions=runtime_versions,
                            ingest_stage=ingest_stage,
                            qa_summary=qa_summary,
                            judge_summary=judge_summary,
                            metrics_result=metrics_result,
                            openclaw_log_path=openclaw_log_path,
                            openviking_log_path=openviking_log_path,
                            observer_snapshot_path=observer_snapshot_path,
                        )
                        write_json(self.manifests_root / f"{run_id}.json", manifest)
                        completed_run_ids.append(run_id)
                    except Exception as exc:
                        end_iso = utc_now_iso_ms()
                        if ov_snapshots and observer_snapshot_path is None:
                            observer_snapshot_path = self.raw_root / "observer" / group_id / f"{sample_id}__r{rerun}.json"
                            write_json(observer_snapshot_path, ov_snapshots)
                        failure_manifest = {
                            "run_id": run_id,
                            "group_id": group_id,
                            "group_short_id": short_group_id(group_id),
                            "group_label": GROUPS[group_id]["label"],
                            "sample_index": sample_index,
                            "sample_id": sample_id,
                            "rerun": rerun,
                            "user": user,
                            "start_time": start_iso,
                            "end_time": end_iso,
                            "success": False,
                            "formal_valid": False,
                            "error": str(exc),
                            "openclaw_log": str(openclaw_log_path) if openclaw_log_path else None,
                            "openviking_log": str(self.logs_root / "openviking" / group_id / f"{sample_id}__r{rerun}.log"),
                            "observer_snapshot": str(observer_snapshot_path) if observer_snapshot_path else None,
                        }
                        write_json(self.manifests_root / f"{run_id}.json", failure_manifest)
                        raise
                    finally:
                        self.stop_gateway(proc)
                        if ov_snapshots and observer_snapshot_path is None:
                            observer_snapshot_path = self.raw_root / "observer" / group_id / f"{sample_id}__r{rerun}.json"
                            write_json(observer_snapshot_path, ov_snapshots)
                        if run_dir is not None:
                            target_ov_log = self.logs_root / "openviking" / group_id / f"{sample_id}__r{rerun}.log"
                            self.copy_openviking_logs(run_dir, target_ov_log)
                            if os.environ.get("EXP_KEEP_RUNS", "0").strip() != "1" and run_dir.exists():
                                shutil.rmtree(run_dir)

        return {
            "dataset_validation": dataset_validation,
            "selected_group_ids": sorted(selected_groups),
            "selected_sample_indices": selected_indices,
            "reruns": reruns,
            "completed_run_ids": completed_run_ids,
        }

    def run(self) -> None:
        preflight = self.preflight()
        repo_commits = self.fetch_repos()
        openclaw_version = self.install_openclaw_cli()
        openviking_version = self.install_openviking_runtime()
        runtime_versions = {
            "openclaw_version": openclaw_version,
            "openviking_version": openviking_version,
        }
        self.assert_locked_versions(openclaw_version, openviking_version)
        self.validate_embedding_model_access()
        self.bootstrap_base_state()
        self.build_templates()
        self.run_smoke_check()
        formal_info = self.run_formal_experiment(repo_commits, runtime_versions)
        summary_payload = summarize(self.artifacts_root)
        overall_manifest = {
            "created_at": utc_now_iso_ms(),
            "started_at": self.started_at,
            "finished_at": utc_now_iso_ms(),
            "openclaw_version": openclaw_version,
            "openviking_version": openviking_version,
            "repo_commits": repo_commits,
            "bootstrap_plugin_source": self.describe_plugin_source(),
            "primary_model_alias": self.primary_model_ref,
            "provider_model_ids_seen": sorted(self.provider_model_ids_seen),
            "judge_model": self.judge_model,
            "judge_provider_model_ids_seen": sorted(self.judge_provider_model_ids_seen),
            "os": platform.platform(),
            "python_version": python_version_string(),
            "node_version": command_version(["node", "--version"]),
            "preflight": preflight,
            "dataset_validation": formal_info["dataset_validation"],
            "formal_run": {
                "selected_group_ids": formal_info["selected_group_ids"],
                "selected_sample_indices": formal_info["selected_sample_indices"],
                "reruns": formal_info["reruns"],
                "completed_run_ids": formal_info["completed_run_ids"],
            },
            "summary_counts": summary_payload.get("counts", {}),
        }
        write_json(self.artifacts_root / "manifest.json", overall_manifest)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenViking × OpenClaw experiment orchestrator.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    orchestrator = ExperimentOrchestrator(root=Path.cwd(), args=args)
    orchestrator.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
