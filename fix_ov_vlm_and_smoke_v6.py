
from __future__ import annotations

from pathlib import Path
import json
import py_compile
import shutil
import sys

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd().resolve()
ORCH = ROOT / "scripts" / "orchestrate.py"
ENV_EXAMPLE = ROOT / ".env.example"
LOCK = ROOT / "VERSIONS.lock.json"

for path in (ORCH, ENV_EXAMPLE, LOCK):
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak6")
        if not backup.exists():
            shutil.copy2(path, backup)

if not ORCH.exists():
    raise SystemExit(f"Missing required file: {ORCH}")

text = ORCH.read_text(encoding="utf-8")

old_init = '''        self.gateway_token = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "").strip() or random_token()
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
'''

new_init = '''        self.gateway_token = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "").strip() or random_token()
        self.ark_llm_endpoint_id = os.environ.get("ARK_LLM_ENDPOINT_ID", "").strip()
        self.ark_ov_vlm_endpoint_id = os.environ.get("ARK_OV_VLM_ENDPOINT_ID", "").strip()
        self.ark_ov_vlm_model = os.environ.get("ARK_OV_VLM_MODEL", "").strip()
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

        defaults = self.lock.setdefault("experiment_defaults", {})
        default_ov_vlm = str(defaults.get("ov_vlm_model", "") or "").strip()
        if default_ov_vlm in {
            "",
            "doubao-seed-2.0-code",
            "doubao-seed-code",
            "seed-2.0-code",
            "volcengine-plan/doubao-seed-code",
            "volcengine-plan/seed-2.0-code",
            "ark-code-latest",
            "volcengine-plan/ark-code-latest",
        }:
            default_ov_vlm = "doubao-seed-2-0-pro-260215"
        ov_vlm_override = self.ark_ov_vlm_endpoint_id or self.ark_ov_vlm_model
        if ov_vlm_override:
            default_ov_vlm = ov_vlm_override
        defaults["ov_vlm_model"] = default_ov_vlm

        if self.ark_embedding_endpoint_id:
            self.ark_embedding_model = self.ark_embedding_endpoint_id
        if self.ark_embedding_model:
            defaults["ov_embedding_model"] = self.ark_embedding_model
'''

if old_init in text:
    text = text.replace(old_init, new_init, 1)
else:
    text = text.replace(
        '        if self.ark_llm_endpoint_id:\n            self.lock.setdefault("experiment_defaults", {})["ov_vlm_model"] = self.ark_llm_endpoint_id\n',
        ''
    )
    if 'self.ark_ov_vlm_endpoint_id' not in text:
        anchor = '        self.ark_llm_endpoint_id = os.environ.get("ARK_LLM_ENDPOINT_ID", "").strip()\n'
        if anchor not in text:
            raise SystemExit("Could not locate ARK_LLM_ENDPOINT_ID line for OV VLM patch.")
        text = text.replace(
            anchor,
            anchor + '        self.ark_ov_vlm_endpoint_id = os.environ.get("ARK_OV_VLM_ENDPOINT_ID", "").strip()\n'
                     '        self.ark_ov_vlm_model = os.environ.get("ARK_OV_VLM_MODEL", "").strip()\n',
            1,
        )
    if 'default_ov_vlm = str(defaults.get("ov_vlm_model"' not in text:
        anchor = '        if self.ark_llm_endpoint_id and not os.environ.get("JUDGE_MODEL", "").strip():\n            self.judge_model = self.ark_llm_endpoint_id\n'
        if anchor not in text:
            raise SystemExit("Could not locate judge-model anchor for OV VLM defaults.")
        snippet = (
            '        defaults = self.lock.setdefault("experiment_defaults", {})\n'
            '        default_ov_vlm = str(defaults.get("ov_vlm_model", "") or "").strip()\n'
            '        if default_ov_vlm in {\n'
            '            "",\n'
            '            "doubao-seed-2.0-code",\n'
            '            "doubao-seed-code",\n'
            '            "seed-2.0-code",\n'
            '            "volcengine-plan/doubao-seed-code",\n'
            '            "volcengine-plan/seed-2.0-code",\n'
            '            "ark-code-latest",\n'
            '            "volcengine-plan/ark-code-latest",\n'
            '        }:\n'
            '            default_ov_vlm = "doubao-seed-2-0-pro-260215"\n'
            '        ov_vlm_override = self.ark_ov_vlm_endpoint_id or self.ark_ov_vlm_model\n'
            '        if ov_vlm_override:\n'
            '            default_ov_vlm = ov_vlm_override\n'
            '        defaults["ov_vlm_model"] = default_ov_vlm\n'
        )
        text = text.replace(anchor, anchor + snippet, 1)

if 'def validate_ov_vlm_model_access(self) -> None:' not in text:
    anchor = '    def preflight(self) -> dict[str, Any]:\n'
    if anchor not in text:
        raise SystemExit("Could not locate preflight() anchor for VLM precheck method.")
    method = '''

    def validate_ov_vlm_model_access(self) -> None:
        vlm_model = str(self.lock.get("experiment_defaults", {}).get("ov_vlm_model", "") or "").strip()
        if not vlm_model:
            raise RuntimeError(
                "OpenViking VLM model is empty. "
                "Set ARK_OV_VLM_MODEL / ARK_OV_VLM_ENDPOINT_ID, or leave the default "
                "doubao-seed-2-0-pro-260215 in place."
            )

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.volcano_api_key}",
        }
        payload = {
            "model": vlm_model,
            "messages": [{"role": "user", "content": "只回复OK"}],
            "max_tokens": 8,
        }
        url = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=30)
            if resp.status_code >= 400:
                detail = (resp.text or "")[:800]
                raise RuntimeError(
                    f"OpenViking VLM probe failed for model {vlm_model} with HTTP {resp.status_code}: {detail}"
                )
            body = resp.json()
        except Exception as exc:
            raise RuntimeError(
                "OpenViking VLM precheck failed. "
                f"Model={vlm_model}. "
                "Use a dedicated semantic-extraction model or endpoint for OpenViking, "
                "for example doubao-seed-2-0-pro-260215 or ARK_OV_VLM_ENDPOINT_ID=ep-... . "
                f"Underlying error: {exc}"
            ) from exc

        choices = body.get("choices") if isinstance(body, dict) else None
        if not isinstance(choices, list) or not choices:
            raise RuntimeError(
                "OpenViking VLM precheck returned an unexpected response. "
                f"Model={vlm_model}. Response={body!r}"
            )
        self.log(f"OpenViking VLM probe succeeded for model {vlm_model}.")
'''
    text = text.replace(anchor, method + anchor, 1)

call_old = '        self.validate_embedding_model_access()\n        self.bootstrap_base_state()\n'
call_new = '        self.validate_embedding_model_access()\n        self.validate_ov_vlm_model_access()\n        self.bootstrap_base_state()\n'
if call_old in text:
    text = text.replace(call_old, call_new, 1)
elif 'self.validate_ov_vlm_model_access()' not in text:
    anchor = '        self.assert_locked_versions(openclaw_version, openviking_version)\n        self.validate_embedding_model_access()\n'
    if anchor not in text:
        raise SystemExit("Could not locate run() preflight anchor for VLM precheck call.")
    text = text.replace(
        anchor,
        anchor + '        self.validate_ov_vlm_model_access()\n',
        1,
    )

old_smoke_block = '''            probe = f"probe-{int(time.time())}-{random.randint(1000, 9999)}"
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
'''

new_smoke_block = '''            probe = f"probe-{int(time.time())}-{random.randint(1000, 9999)}"
            prefix = f"[OV-BENCH-SMOKE][{probe}] "
            user = f"ov-smoke-{int(time.time())}"
            msgs = [
                prefix
                + "Remember these benchmark-smoke facts: "
                "my name is Lin Zhou, I am rebuilding an order platform, "
                "my backend stack is Go, PostgreSQL, and Redis, and the current project progress is 70 percent. "
                "Reply briefly.",
                prefix
                + "More benchmark-smoke facts: "
                "our Kafka topic is order_events_v2, "
                "the payment callback service runs on payment-cb.internal:9443, "
                "and the main latency alert is P99 over 450ms for 3 minutes.",
                prefix
                + "Additional benchmark-smoke facts: "
                "the inventory service exhausted its connection pool. "
                "We fixed it by raising max_open_conns from 80 to 160 and by adding a circuit breaker.",
                prefix
                + "Benchmark-smoke preference for this session: "
                "keep answers concise, put the conclusion first, then the reason if needed.",
            ]
'''

if old_smoke_block in text:
    text = text.replace(old_smoke_block, new_smoke_block, 1)
else:
    if '"All data below is synthetic and should be ignored after the check completes. "' in text:
        text = text.replace('"All data below is synthetic and should be ignored after the check completes. "', '""')

text = text.replace(
    '"[OPENVIKING-HEALTHCHECK] Based on the synthetic test data above, "\n                    "summarize the backend stack and current project progress in one short sentence."\n',
    '"[OV-BENCH-SMOKE] Based on the benchmark-smoke facts above, "\n                    "summarize the backend stack and current project progress in one short sentence."\n',
)
text = text.replace(
    '"[OPENVIKING-HEALTHCHECK] Based on the synthetic test data from the healthcheck, "\n                    "reply with the Kafka topic and payment callback service address in one line."\n',
    '"[OV-BENCH-SMOKE] Based on the benchmark-smoke facts from earlier, "\n                    "reply with the Kafka topic and payment callback service address in one line."\n',
)

ORCH.write_text(text, encoding="utf-8")

if LOCK.exists():
    data = json.loads(LOCK.read_text(encoding="utf-8"))
    defaults = data.setdefault("experiment_defaults", {})
    if str(defaults.get("ov_vlm_model", "")).strip() in {
        "",
        "doubao-seed-2.0-code",
        "doubao-seed-code",
        "seed-2.0-code",
        "volcengine-plan/doubao-seed-code",
        "volcengine-plan/seed-2.0-code",
        "ark-code-latest",
        "volcengine-plan/ark-code-latest",
    }:
        defaults["ov_vlm_model"] = "doubao-seed-2-0-pro-260215"
    LOCK.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

if ENV_EXAMPLE.exists():
    env_text = ENV_EXAMPLE.read_text(encoding="utf-8")
    if 'ARK_OV_VLM_MODEL=' not in env_text:
        addition = (
            '\n# OpenViking VLM (memory extraction) should stay separate from the main Gateway model.\n'
            '# Recommended direct model id:\n'
            'ARK_OV_VLM_MODEL=doubao-seed-2-0-pro-260215\n'
            '# Or point OpenViking extraction at a dedicated Ark endpoint:\n'
            '# ARK_OV_VLM_ENDPOINT_ID=ep-xxxxxxxxxxxxxxxx\n'
        )
        env_text += addition
    ENV_EXAMPLE.write_text(env_text, encoding="utf-8")

py_compile.compile(str(ORCH), doraise=True)
print(f"Patched successfully: {ROOT}")
print("Backups saved as *.bak6")
print("This patch adds: dedicated OV VLM selection/precheck, benchmark-safe smoke prompts, and a saner default ov_vlm_model.")
