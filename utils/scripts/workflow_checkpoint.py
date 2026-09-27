"""Validated state transitions with atomic checkpoints for local Python workflows."""
from __future__ import annotations

from pathlib import Path

from utils.scripts.run_state import write_json
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
    def __init__(self, transitions: dict[str, tuple[str, ...]], run_dir: Path | None = None):
        self.transitions = transitions
        self.run_dir = run_dir
        self.state = "prepared"
        self.history = [self.state]
        self.details: dict = {}
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
