from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import AsyncOpenAI

from .common import append_jsonl, ensure_dir, read_json, write_json

SYSTEM_PROMPT = """You are a careful benchmark grader.
Return JSON only.
"""

JUDGE_PROMPT_VERSION = "openviking-openclaw-locomo-v2-2026-04-15"


def build_accuracy_prompt(question: str, gold_answer: str, response: str) -> str:
    return f"""
You are grading one answer from a long-memory benchmark.

Label the generated answer as CORRECT or WRONG.

Be generous on wording:
- If the generated answer clearly refers to the same entity, event, fact, or topic as the gold answer, mark CORRECT.
- For time questions, different surface forms are okay if they point to the same date or time period.
- Ignore extra irrelevant text unless it changes the core meaning.
- If the answer is empty, evasive, or factually incompatible with the gold answer, mark WRONG.

Question: {question}
Gold answer: {gold_answer}
Generated answer: {response}

Return JSON only in this schema:
{{
  "is_correct": "CORRECT" or "WRONG",
  "reasoning": "one short sentence"
}}
""".strip()


def usage_to_dict(usage: Any) -> dict[str, int]:
    if usage is None:
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    if isinstance(usage, dict):
        return {
            "input_tokens": int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0),
            "output_tokens": int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
        }
    prompt_tokens = getattr(usage, "prompt_tokens", getattr(usage, "input_tokens", 0)) or 0
    completion_tokens = getattr(usage, "completion_tokens", getattr(usage, "output_tokens", 0)) or 0
    total_tokens = getattr(usage, "total_tokens", 0) or (prompt_tokens + completion_tokens)
    return {
        "input_tokens": int(prompt_tokens),
        "output_tokens": int(completion_tokens),
        "total_tokens": int(total_tokens),
    }


def parse_judge_text(text: str) -> tuple[bool, dict[str, Any]]:
    cleaned = (text or "").strip()
    parsed: dict[str, Any] = {}
    if cleaned:
        try:
            parsed = json.loads(cleaned)
        except Exception:
            match = re.search(r"\{.*\}", cleaned, flags=re.S)
            if match:
                try:
                    parsed = json.loads(match.group(0))
                except Exception:
                    parsed = {}
    label = str(parsed.get("is_correct", parsed.get("label", ""))).strip().lower()
    if not label and "correct" in cleaned.lower() and "wrong" not in cleaned.lower():
        label = "correct"
    is_correct = label == "correct"
    reasoning = parsed.get("reasoning", "")
    if not isinstance(reasoning, str):
        reasoning = json.dumps(reasoning, ensure_ascii=False)
    return is_correct, {
        "raw_text": cleaned,
        "parsed_json": parsed,
        "reasoning": reasoning,
        "label": label,
    }


def load_answers(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".jsonl":
        answers: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                answers.append(json.loads(line))
        return answers
    data = read_json(path, default=[])
    if isinstance(data, dict):
        if "results" in data and isinstance(data["results"], list):
            return data["results"]
        if "records" in data and isinstance(data["records"], list):
            return data["records"]
    if isinstance(data, list):
        return data
    raise RuntimeError(f"Unsupported answers file: {path}")


async def grade_one(
    *,
    client: AsyncOpenAI,
    model: str,
    record: dict[str, Any],
    semaphore: asyncio.Semaphore,
    retries: int = 2,
) -> dict[str, Any]:
    question = str(record.get("question", ""))
    gold_answer = str(record.get("gold_answer", record.get("expected", "")))
    response_text = str(record.get("prediction", record.get("response", "")))
    prompt = build_accuracy_prompt(
        question=question,
        gold_answer=gold_answer,
        response=response_text,
    )
    async with semaphore:
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                response = await client.chat.completions.create(
                    model=model,
                    temperature=0,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                )
                text = response.choices[0].message.content or ""
                usage = usage_to_dict(getattr(response, "usage", None))
                is_correct, parsed = parse_judge_text(text)
                judge_record = {
                    **record,
                    "grade": bool(is_correct),
                    "judge_correct": bool(is_correct),
                    "judge_model": model,
                    "judge_provider_model_id": getattr(response, "model", model),
                    "judge_prompt_version": JUDGE_PROMPT_VERSION,
                    "judge_prompt": prompt,
                    "judge_usage": usage,
                    "judge_raw_text": parsed["raw_text"],
                    "judge_reasoning": parsed.get("reasoning", ""),
                    "judge_reasoning_raw": parsed.get("reasoning", ""),
                    "judge_label": parsed.get("label", ""),
                    "judge_parsed_json": parsed.get("parsed_json", {}),
                    "judge_result_raw": parsed.get("parsed_json", {}),
                }
                return judge_record
            except Exception as exc:
                last_exc = exc
                if attempt < retries:
                    await asyncio.sleep(1.0 + attempt)
        assert last_exc is not None
        return {
            **record,
            "grade": False,
            "judge_correct": False,
            "judge_model": model,
            "judge_provider_model_id": model,
            "judge_prompt_version": JUDGE_PROMPT_VERSION,
            "judge_prompt": prompt,
            "judge_usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "judge_raw_text": "",
            "judge_reasoning": f"[JUDGE_ERROR] {last_exc}",
            "judge_reasoning_raw": f"[JUDGE_ERROR] {last_exc}",
            "judge_label": "error",
            "judge_parsed_json": {},
            "judge_result_raw": {},
        }


async def grade_answers(
    *,
    answers_path: Path,
    output_json: Path,
    output_raw_jsonl: Path,
    base_url: str | None,
    api_key: str | None,
    model: str,
    concurrency: int = 16,
) -> dict[str, Any]:
    load_dotenv()
    client = AsyncOpenAI(
        base_url=base_url or os.getenv("OPENAI_BASE_URL"),
        api_key=api_key or os.getenv("OPENAI_API_KEY"),
    )
    answers = load_answers(answers_path)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    tasks = [
        grade_one(client=client, model=model, record=record, semaphore=semaphore)
        for record in answers
    ]
    results = await asyncio.gather(*tasks)

    ensure_dir(output_raw_jsonl.parent)
    if output_raw_jsonl.exists():
        output_raw_jsonl.unlink()
    for item in results:
        append_jsonl(output_raw_jsonl, item)

    correct = sum(1 for item in results if item["judge_correct"])
    total = len(results)
    score = correct / total if total else 0.0

    categories: dict[str, dict[str, Any]] = {}
    for item in results:
        cat = str(item.get("category", "unknown"))
        bucket = categories.setdefault(cat, {"correct": 0, "total": 0})
        bucket["total"] += 1
        if item["judge_correct"]:
            bucket["correct"] += 1
    for bucket in categories.values():
        bucket["score"] = bucket["correct"] / bucket["total"] if bucket["total"] else 0.0

    judge_usage_total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for item in results:
        usage = item.get("judge_usage", {})
        for key in judge_usage_total:
            judge_usage_total[key] += int(usage.get(key, 0) or 0)

    summary = {
        "answers_path": str(answers_path),
        "judge_model": model,
        "judge_prompt_version": JUDGE_PROMPT_VERSION,
        "score": score,
        "correct": correct,
        "total": total,
        "categories": categories,
        "judge_usage_total": judge_usage_total,
        "grades": results,
    }
    write_json(output_json, summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LLM-as-a-judge harness.")
    parser.add_argument("answers", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--raw-output", type=Path, required=True)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--model", required=True)
    parser.add_argument("--concurrency", type=int, default=16)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    summary = asyncio.run(
        grade_answers(
            answers_path=args.answers,
            output_json=args.output,
            output_raw_jsonl=args.raw_output,
            base_url=args.base_url,
            api_key=args.token,
            model=args.model,
            concurrency=args.concurrency,
        )
    )
    print(
        json.dumps(
            {
                "correct": summary["correct"],
                "total": summary["total"],
                "score": summary["score"],
                "judge_prompt_version": summary["judge_prompt_version"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
