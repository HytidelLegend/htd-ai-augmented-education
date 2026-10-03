"""Validated state transitions with atomic checkpoints for local Python workflows."""
from __future__ import annotations

from pathlib import Path

from utils.scripts.run_state import read_json, write_json
from utils.scripts.timestamp import iso_timestamp, unique_filename_timestamp


def create_run_directory(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    while True:
        candidate = root / unique_filename_timestamp(p.name for p in root.iterdir())
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            continue


class WorkflowCheckpoint:
    def __init__(self, transitions: dict[str, tuple[str, ...]], run_dir: Path | None = None, *, resume: bool = False):
        self.transitions = transitions
        self.run_dir = run_dir
        self.state = "prepared"
        self.history = [self.state]
        self.details: dict = {}
        if resume and run_dir and (run_dir / 'state.json').is_file():
            saved = read_json(run_dir / 'state.json')
            state, history = saved.get('state'), saved.get('stateHistory')
            if (not isinstance(state, str) or state not in transitions or not isinstance(history, list) or not history
                    or history[0] != 'prepared' or history[-1] != state
                    or any(not isinstance(item, str) for item in history)
                    or any(after not in transitions.get(before, ()) for before, after in zip(history, history[1:]))):
                raise ValueError('状态机检查点或迁移历史无效')
            # A completed publication begins a new cycle; unfinished work keeps its history.
            if state != 'completed':
                self.state, self.history = state, history
                self.details = {key: value for key, value in saved.items()
                                if key not in ('state', 'stateHistory', 'updatedAt')}
                return
        self._write()

    def move(self, state: str, **details: object) -> None:
        if state not in self.transitions.get(self.state, ()):
            raise ValueError(f"Illegal transition: {self.state} -> {state}")
        self.state = state
        self.history.append(state)
        self.details.update(details)
        self._write()

    def update(self, **details: object) -> None:
        """Persist details discovered while staying in the current state."""
        self.details.update(details)
        self._write()

    def _write(self) -> None:
        if self.run_dir:
            write_json(self.run_dir / "state.json", {
                "state": self.state, "stateHistory": self.history,
                "updatedAt": iso_timestamp(), **self.details,
            })
