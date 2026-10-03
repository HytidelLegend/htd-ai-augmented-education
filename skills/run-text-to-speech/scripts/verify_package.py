"""Verify integrated skill registration and optional local environment."""
from pathlib import Path
import argparse
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from utils.scripts.speech_cli import verify_installation
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment", action="store_true")
    args = parser.parse_args()
    result = verify_installation(Path(__file__).resolve().parents[1], args.environment)
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["status"] == "passed" else 6)
