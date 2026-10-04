"""Project CLI for convert-copy-to-transcript."""
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from utils.scripts.speech_cli import invoke
if __name__ == "__main__":
    if '--mode' in sys.argv and sys.argv.index('--mode') + 1 < len(sys.argv) and sys.argv[sys.argv.index('--mode') + 1] == 'qa-dialogue':
        from utils.scripts.dialogue_pipeline import main
        raise SystemExit(main('convert', sys.argv[1:]))
    raise SystemExit(invoke(Path(__file__).resolve().parents[1], 'convert_copy_to_transcript.py', sys.argv[1:], start_command='prepare'))
