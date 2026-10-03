"""Verify every user-requested voice name through the public resolver."""
import importlib.util
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("portable_voice_runner", Path(__file__).resolve().parents[1] / "scripts/run_tts.py")
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)


@pytest.mark.parametrize("name,speaker", [
    ("Hytidel（正常说话）", "S_IBxS5KRZ1"),
    ("Hytidel", "S_IBxS5KRZ1"),
    ("大雄", "S_NBxS5KRZ1"),
    ("哆啦A梦", "S_QBxS5KRZ1"),
])
def test_named_voice_mapping(name, speaker):
    assert runner.resolve_voice(name, None, {**runner.load_config(), "backend": "volcengine"}) == (name, speaker, "seed-icl-2.0")
