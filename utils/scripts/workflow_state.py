"""Shared, schema-validated state transitions for recoverable project workflows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from jsonschema import Draft202012Validator

try:
    from .file_transaction import project_lock
    from .jsonl_store import append_jsonl
    from .structured_io import read_json, write_json
    from .timestamp import iso_timestamp
except ImportError:  # Support project scripts that add utils/scripts directly to sys.path.
    from file_transaction import project_lock
    from jsonl_store import append_jsonl
    from structured_io import read_json, write_json
    from timestamp import iso_timestamp


class WorkflowStateError(ValueError):
    """Raised when a workflow state or transition is invalid."""


@dataclass(frozen=True)
class WorkflowDefinition:
    """Declare legal status transitions and terminal states for one workflow."""

    name: str
    transitions: Mapping[str, frozenset[str]]
    terminal_statuses: frozenset[str] = frozenset({"completed", "superseded", "failed_terminal"})

    @classmethod
    def build(
        cls,
        *,
        name: str,
        transitions: Mapping[str, Iterable[str]],
        terminal_statuses: Iterable[str] = ("completed", "superseded", "failed_terminal"),
    ) -> "WorkflowDefinition":
        return cls(
            name=name,
            transitions={key: frozenset(values) for key, values in transitions.items()},
            terminal_statuses=frozenset(terminal_statuses),
        )

    @property
    def statuses(self) -> frozenset[str]:
        values = set(self.transitions)
        for targets in self.transitions.values():
            values.update(targets)
        values.update(self.terminal_statuses)
        return frozenset(values)


class WorkflowStateStore:
    """Persist one workflow state with legal transitions, events, and a run lock."""

    def __init__(
        self,
        *,
        root: Path,
        workflow: str,
        run_id: str,
        definition: WorkflowDefinition,
        schema_path: Path | None = None,
        run_dir: Path | None = None,
        state_dir: Path | None = None,
        events_dir: Path | None = None,
    ) -> None:
        self.root = root.resolve()
        self.workflow = workflow
        self.run_id = run_id
        self.definition = definition
        if workflow != definition.name:
            raise WorkflowStateError("workflow 与状态机定义名称不一致")
        self.run_dir = run_dir.resolve() if run_dir else self.root / "logs" / workflow / "runs" / run_id
        self.state_dir = state_dir.resolve() if state_dir else self.run_dir
        self.path = self.state_dir / "state.json"
        self.lock_path = self.state_dir / "state.lock"
        self.events_dir = events_dir.resolve() if events_dir else self.root / "logs" / workflow / "events"
        default_schema = self.root / "utils" / "references" / "workflow-state-v1.schema.json"
        self.schema_path = schema_path or default_schema
        schema = read_json(self.schema_path)
        self.validator = Draft202012Validator(schema)

    def lock(self):
        return project_lock(self.lock_path, f"{self.workflow}:{self.run_id}")

    def _validate(self, state: dict[str, Any]) -> None:
        errors = sorted(self.validator.iter_errors(state), key=lambda item: list(item.path))
        if errors:
            rendered = "；".join(error.message for error in errors)
            raise WorkflowStateError(f"状态 Schema 校验失败：{rendered}")
        if state.get("workflow") != self.workflow or state.get("run_id") != self.run_id:
            raise WorkflowStateError("状态 workflow 或 run_id 与存储位置不一致")
        if state.get("status") not in self.definition.statuses:
            raise WorkflowStateError(f"未知状态：{state.get('status')}")

    def create(self, state: dict[str, Any]) -> dict[str, Any]:
        if self.path.exists():
            raise WorkflowStateError(f"运行状态已存在，不覆盖：{self.path}")
        self._validate(state)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        write_json(self.path, state)
        self._append_event(
            state,
            event="run_initialized",
            from_status=None,
            to_status=state["status"],
        )
        write_json(self.path, state)
        return state

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            raise WorkflowStateError(f"找不到运行检查点：{self.run_id}")
        state = read_json(self.path)
        if not isinstance(state, dict):
            raise WorkflowStateError("运行状态必须是 JSON 对象")
        self._validate(state)
        return state

    def save(self, state: dict[str, Any]) -> dict[str, Any]:
        self._validate(state)
        write_json(self.path, state)
        return state

    def _append_event(
        self,
        state: dict[str, Any],
        *,
        event: str,
        from_status: str | None = None,
        to_status: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        state["event_sequence"] = int(state.get("event_sequence", 0)) + 1
        now = iso_timestamp()
        payload = {
            "timestamp": now,
            "run_id": self.run_id,
            "sequence": state["event_sequence"],
            "event": event,
            "from_status": from_status,
            "to_status": to_status or state["status"],
            "current_stage": state["current_stage"],
            "current_batch_id": state.get("current_batch_id"),
            "details": details or {},
        }
        append_jsonl(
            self.events_dir / f"{now[:10]}.jsonl",
            [payload],
            key_fields=("run_id", "sequence"),
        )

    def checkpoint(
        self,
        state: dict[str, Any],
        *,
        event: str,
        completed_step: str | None = None,
        updates: Mapping[str, Any] | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if updates:
            state.update(dict(updates))
        if completed_step and completed_step not in state["completed_steps"]:
            state["completed_steps"].append(completed_step)
        now = iso_timestamp()
        state["updated_at"] = now
        state["last_heartbeat_at"] = now
        self._append_event(state, event=event, details=details)
        return self.save(state)

    def transition(
        self,
        state: dict[str, Any],
        target: str,
        *,
        stage: str | None = None,
        completed_step: str | None = None,
        updates: Mapping[str, Any] | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = str(state["status"])
        if current == target:
            if current in self.definition.terminal_statuses:
                return state
            raise WorkflowStateError(f"拒绝重复的非终态迁移：{current}")
        allowed = self.definition.transitions.get(current, frozenset())
        if target not in allowed:
            raise WorkflowStateError(f"非法状态迁移：{current} → {target}")
        if completed_step and completed_step not in state["completed_steps"]:
            state["completed_steps"].append(completed_step)
        state["status"] = target
        if stage is not None:
            state["current_stage"] = stage
        if not target.startswith("paused_"):
            state["resume_stage"] = None
            state["pending_decisions"] = []
            if target != "failed_terminal":
                state["error"] = None
        if updates:
            state.update(dict(updates))
        now = iso_timestamp()
        state["updated_at"] = now
        state["last_heartbeat_at"] = now
        self._append_event(
            state,
            event="state_transition",
            from_status=current,
            to_status=target,
            details=details,
        )
        return self.save(state)

    def pause(
        self,
        state: dict[str, Any],
        *,
        status: str,
        error_code: str,
        message: str,
        resume_stage: str,
        details: Any = None,
        pending_decisions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not status.startswith("paused_"):
            raise WorkflowStateError("暂停状态必须以 paused_ 开头")
        return self.transition(
            state,
            status,
            updates={
                "resume_stage": resume_stage,
                "error": {"code": error_code, "message": message, "details": details},
                "pending_decisions": pending_decisions or [],
            },
            details={"error_code": error_code, "message": message},
        )

    def resume(self, state: dict[str, Any]) -> dict[str, Any]:
        current = str(state["status"])
        target = state.get("resume_stage")
        if not current.startswith("paused_") or not isinstance(target, str) or not target:
            raise WorkflowStateError("当前状态没有可恢复阶段")
        return self.transition(state, target, stage=target, details={"resumed_from": current})

    def supersede(self, state: dict[str, Any], *, reason: str) -> dict[str, Any]:
        if not reason.strip():
            raise WorkflowStateError("supersede 原因不能为空")
        return self.transition(
            state,
            "superseded",
            updates={"superseded_reason": reason.strip()},
            details={"reason": reason.strip()},
        )
