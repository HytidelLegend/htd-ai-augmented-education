"""A small, resumable binary-search diagnostic over a curriculum order.

Questions are authored by the teaching agent and answered one at a time.  A
correct answer moves the lower bound; an incorrect or unsure answer moves the
upper bound.  The result is deliberately an estimate, not a mastery claim.
"""

from __future__ import annotations

from typing import Any


def target(assessment: dict[str, Any], units: list[dict[str, Any]]) -> dict[str, Any] | None:
    if assessment["status"] == "completed":
        return None
    lower, upper = assessment["known_through"], assessment["unknown_from"]
    if upper - lower <= 1 or len(assessment["answers"]) >= assessment["max_questions"]:
        return None
    index = (lower + upper) // 2
    return {"index": index, "unit_id": units[index]["unit_id"], "title": units[index]["title"]}


def begin(units: list[dict[str, Any]], current_level: str, learning_goal: str, max_questions: int = 5) -> dict[str, Any]:
    if not current_level.strip() or not learning_goal.strip():
        raise ValueError("开始学习前必须确认当前学习水平和学习目的")
    if not units:
        raise ValueError("没有可用于诊断的学习单元")
    result = {
        "schema_version": "1.0", "status": "question_required", "current_level": current_level.strip(),
        "learning_goal": learning_goal.strip(), "known_through": -1, "unknown_from": len(units),
        "max_questions": max_questions, "answers": [], "pending_question": None,
    }
    if target(result, units) is None:
        result["status"] = "completed"
    return result


def add_question(assessment: dict[str, Any], units: list[dict[str, Any]], question: dict[str, Any]) -> None:
    wanted = target(assessment, units)
    if wanted is None or assessment["pending_question"] is not None:
        raise ValueError("当前不需要新题目")
    if question.get("unit_id") != wanted["unit_id"]:
        raise ValueError("题目必须针对当前二分探测单元")
    options = question.get("options")
    if (not isinstance(question.get("prompt"), str) or not question["prompt"].strip()
            or not isinstance(options, list) or len(options) != 4
            or any(not isinstance(x, str) or not x.strip() for x in options)
            or len(set(options)) != 4 or question.get("correct_index") not in range(4)):
        raise ValueError("题目需要非空题干、四个不同选项及 0–3 的正确选项索引")
    assessment["pending_question"] = {**question, "index": wanted["index"]}
    assessment["status"] = "awaiting_answer"


def answer(assessment: dict[str, Any], units: list[dict[str, Any]], choice: int | None) -> dict[str, Any]:
    question = assessment.get("pending_question")
    if assessment["status"] != "awaiting_answer" or question is None:
        raise ValueError("当前没有待回答的诊断题")
    if choice is not None and choice not in range(4):
        raise ValueError("答案必须是 0–3，或用 unsure 表示不确定")
    correct = choice is not None and choice == question["correct_index"]
    index = question["index"]
    assessment["answers"].append({"unit_id": question["unit_id"], "index": index,
                                  "choice": choice, "correct": correct})
    if correct:
        assessment["known_through"] = max(assessment["known_through"], index)
    else:
        assessment["unknown_from"] = min(assessment["unknown_from"], index)
    assessment["pending_question"] = None
    next_target = target(assessment, units)
    assessment["status"] = "question_required" if next_target else "completed"
    return {"status": assessment["status"], "next_target": next_target,
            "boundary_after_unit_id": units[assessment["known_through"]]["unit_id"] if assessment["known_through"] >= 0 else None,
            "first_uncertain_unit_id": units[assessment["unknown_from"]]["unit_id"] if assessment["unknown_from"] < len(units) else None}
