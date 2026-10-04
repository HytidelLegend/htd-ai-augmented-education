"""Synchronize the word-entry overview using the shared capability catalog."""
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from utils.scripts.capability_catalog import generate

def main():
    return generate(ROOT, check='--check' in sys.argv)

if __name__ == '__main__':
    main()
