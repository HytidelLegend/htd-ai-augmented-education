from __future__ import annotations

import importlib.util
from pathlib import Path


def load_runner():
    root = Path(__file__).resolve().parents[3]
    module_path = root / "skills/interactive-tutor/scripts/run_interactive_tutor.py"
    spec = importlib.util.spec_from_file_location("beta_interactive_tutor_runner", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
