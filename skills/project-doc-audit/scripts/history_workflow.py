"""Compatibility entry point for the shared version workflow."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from utils.scripts.version_workflow import *
if __name__ == "__main__":
    raise SystemExit(main())
