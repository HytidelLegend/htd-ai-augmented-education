"""Deterministic batch scheduling for day-based spaced recall plans."""

from __future__ import annotations

from math import ceil
from typing import Any


DEFAULT_REVIEW_OFFSETS = (1, 2, 4, 7, 15)
COMPLETION_MODES = ("first_pass", "one_review")


class ScheduleInfeasible(ValueError):
    """A valid request cannot meet its deadline."""


def choose_batch_size(request: dict[str, Any]) -> int:
    """Return the smallest batch size under the one-new-batch-per-day rule."""
    n = request["n"]
    d = request.get("d")
    daily_items = request.get("daily_items")
    mode = request.get("completion_mode", "first_pass")
    offsets = request.get("review_offsets", list(DEFAULT_REVIEW_OFFSETS))
    if type(n) is not int or n < 1 or (d is None) == (daily_items is None):
        raise ValueError("n 与 d/daily_items 二选一必须是正整数")
    if d is not None and (type(d) is not int or d < 1):
        raise ValueError("d 必须是正整数")
    if d is not None and mode == "first_pass" and d > n:
        raise ScheduleInfeasible("首次背诵天数不能超过 item 数")
    if daily_items is not None and (type(daily_items) is not int or daily_items < 1):
        raise ValueError("daily_items 必须是正整数")
    if mode not in COMPLETION_MODES:
        raise ValueError("未知完成标准")
    if not isinstance(offsets, list) or not offsets or any(type(x) is not int or x < 1 for x in offsets):
        raise ValueError("review_offsets 必须是非空的正整数数组")
    if offsets != sorted(set(offsets)):
        raise ValueError("review_offsets 必须严格递增且不得重复")

    if daily_items is not None:
        return daily_items
    latest_new_day = d if mode == "first_pass" else d - offsets[0]
    if latest_new_day < 1:
        raise ScheduleInfeasible("期限不足以完成首次背诵及至少一轮复习")
    return ceil(n / latest_new_day)


def build_schedule(request: dict[str, Any]) -> dict[str, Any]:
    """Place one new contiguous batch on each active day, then all due reviews."""
    n = request["n"]
    d = request.get("d")
    mode = request.get("completion_mode", "first_pass")
    offsets = request.get("review_offsets", list(DEFAULT_REVIEW_OFFSETS))
    batch_size = choose_batch_size(request)
    if d is None:
        first_pass_days = ceil(n / batch_size)
        d = first_pass_days if mode == "first_pass" else first_pass_days + offsets[0]
    batches = []
    new_by_day: dict[int, list[dict[str, Any]]] = {}
    review_by_day: dict[int, list[dict[str, Any]]] = {}
    first_pass_days = d if request.get("d") is not None and mode == "first_pass" else ceil(n / batch_size)
    next_item = 1
    for batch_id in range(1, first_pass_days + 1):
        count = (n // first_pass_days + (1 if batch_id <= n % first_pass_days else 0)) if request.get("d") is not None and mode == "first_pass" else min(batch_size, n - next_item + 1)
        if count <= 0:
            break
        start = next_item
        next_item += count
        batch_id = len(batches) + 1
        batch = {
            "batch_id": batch_id,
            "item_ids": list(range(start, start + count)),
            "new_day": batch_id,
        }
        batches.append(batch)
        new_by_day[batch_id] = [batch]
        for offset in offsets:
            review_by_day.setdefault(batch_id + offset, []).append(batch)

    last_day = max(d, len(batches) + offsets[-1])
    days = []
    for day in range(1, last_day + 1):
        new_batches = new_by_day.get(day, [])
        review_batches = review_by_day.get(day, [])
        new_items = [item for batch in new_batches for item in batch["item_ids"]]
        review_items = [item for batch in review_batches for item in batch["item_ids"]]
        days.append({
            "day": day,
            "new_batch_ids": [batch["batch_id"] for batch in new_batches],
            "review_batch_ids": [batch["batch_id"] for batch in review_batches],
            "new_item_ids": new_items,
            "review_item_ids": review_items,
            "item_ids": sorted(set(new_items + review_items)),
        })
    return {
        "schema_version": "1.0",
        "n": n,
        "d": d,
        "completion_mode": mode,
        "review_offsets": offsets,
        "batch_size": batch_size,
        "batches": batches,
        "days": days,
    }


def render_markdown(result: dict[str, Any]) -> str:
    """Render the verified JSON as a readable, continuous daily plan."""
    label = "首次背诵" if result["completion_mode"] == "first_pass" else "首次背诵并至少复习一轮"
    lines = [
        "# 艾宾浩斯背诵计划", "",
        f"- 编号范围：1–{result['n']}",
        f"- 完成期限：第 {result['d']} 天",
        f"- 完成标准：{label}",
        f"- 每批最多：{result['batch_size']} 个 item",
        f"- 复习间隔：{', '.join(str(x) for x in result['review_offsets'])} 天", "",
    ]
    for entry in result["days"]:
        suffix = "（期限后复习）" if entry["day"] > result["d"] else ""
        lines.extend([
            f"## 第 {entry['day']} 天{suffix}", "",
            f"- 首次背诵：{_numbers(entry['new_item_ids'])}",
            f"- 复习：{_numbers(entry['review_item_ids'])}",
            f"- 当日全部：{_numbers(entry['item_ids'])}", "",
        ])
    return "\n".join(lines)


def _numbers(values: list[int]) -> str:
    return ", ".join(str(value) for value in values) if values else "无"
