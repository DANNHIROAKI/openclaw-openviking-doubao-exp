from __future__ import annotations

from typing import Any


EXPECTED_SAMPLE_COUNT = 10
EXPECTED_CASE_COUNT = 1540
CANONICAL_TAIL_TEXT = "[remember what's said, keep existing memory]"

GROUP_ORDER = [
    "g1-ov-nomemory",
    "g2-noov-stock",
    "g3-ov-stock",
]

GROUPS: dict[str, dict[str, Any]] = {
    "g1-ov-nomemory": {
        "short_id": "G1",
        "label": "OV / no-memory",
        "plugins.slots.memory": "none",
        "plugins.slots.contextEngine": "openviking",
        "plugins.entries.openviking.enabled": True,
        "plugins.deny": [],
        "is_ov_group": True,
    },
    "g2-noov-stock": {
        "short_id": "G2",
        "label": "No-OV / stock",
        "plugins.slots.memory": "memory-core",
        "plugins.slots.contextEngine": "legacy",
        "plugins.entries.openviking.enabled": False,
        "plugins.deny": ["openviking"],
        "is_ov_group": False,
    },
    "g3-ov-stock": {
        "short_id": "G3",
        "label": "OV / stock",
        "plugins.slots.memory": "memory-core",
        "plugins.slots.contextEngine": "openviking",
        "plugins.entries.openviking.enabled": True,
        "plugins.deny": [],
        "is_ov_group": True,
    },
}

GROUP_BY_SHORT_ID = {meta["short_id"]: slug for slug, meta in GROUPS.items()}

PLANNED_COMPARISONS = [
    {
        "comparison_id": "C1",
        "label": "C1: G1 vs G2",
        "comparison_nature": "部署比较",
        "treatment_group": "g1-ov-nomemory",
        "control_group": "g2-noov-stock",
    },
    {
        "comparison_id": "C2",
        "label": "C2: G3 vs G2",
        "comparison_nature": "单因素核心比较",
        "treatment_group": "g3-ov-stock",
        "control_group": "g2-noov-stock",
    },
    {
        "comparison_id": "C3",
        "label": "C3: G3 vs G1",
        "comparison_nature": "OV 内部 memory-core 比较",
        "treatment_group": "g3-ov-stock",
        "control_group": "g1-ov-nomemory",
    },
]


def rotated_group_order(sample_index: int, rerun_id: int = 1) -> list[str]:
    shift = (sample_index + max(0, rerun_id - 1)) % len(GROUP_ORDER)
    return GROUP_ORDER[shift:] + GROUP_ORDER[:shift]


def deterministic_user_key(group_id: str, rerun_id: int, sample_id: str) -> str:
    return f"{group_id}-run{rerun_id}-{sample_id}"


def is_ov_group(group_id: str) -> bool:
    return bool(GROUPS[group_id]["is_ov_group"])


def short_group_id(group_id: str) -> str:
    return str(GROUPS[group_id]["short_id"])


def config_snapshot_basename(group_id: str) -> str:
    return f"group-{group_id}"
