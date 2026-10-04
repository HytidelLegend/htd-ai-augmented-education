"""Project-level two-person podcast CLI."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from utils.scripts.dialogue_pipeline import main

if __name__ == '__main__':
    raise SystemExit(main('podcast'))
