from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from .common import command_version, python_version_string


REQUIRED_COMMANDS = ["bash", "git", "curl", "node", "npm", "python3"]


def check_requirements() -> dict[str, object]:
    missing = [cmd for cmd in REQUIRED_COMMANDS if shutil.which(cmd) is None]
    python_ok = sys.version_info >= (3, 10)
    return {
        "python": python_version_string(),
        "python_ok": python_ok,
        "commands": {cmd: bool(shutil.which(cmd)) for cmd in REQUIRED_COMMANDS},
        "missing_commands": missing,
        "env": {
            "VOLCANO_ENGINE_API_KEY": bool(os.environ.get("VOLCANO_ENGINE_API_KEY")),
            "OPENCLAW_GATEWAY_TOKEN": bool(os.environ.get("OPENCLAW_GATEWAY_TOKEN")),
            "JUDGE_API_KEY": bool(os.environ.get("JUDGE_API_KEY")),
        },
        "versions": {
            "node": command_version(["node", "--version"]),
            "npm": command_version(["npm", "--version"]),
            "git": command_version(["git", "--version"]),
            "curl": command_version(["curl", "--version"]),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight checks for experiment bundle.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = check_requirements()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["python_ok"]:
        return 1
    if report["missing_commands"]:
        return 1
    if not report["env"]["VOLCANO_ENGINE_API_KEY"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
