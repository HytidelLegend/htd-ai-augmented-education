"""Project CLI for convert-copy-to-transcript."""
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from utils.scripts.speech_cli import invoke
if __name__ == "__main__":
    raise SystemExit(invoke(Path(__file__).resolve().parents[1], 'convert_copy_to_transcript.py', sys.argv[1:], start_command='prepare'))
