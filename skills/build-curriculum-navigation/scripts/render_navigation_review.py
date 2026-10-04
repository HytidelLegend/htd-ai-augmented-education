"""Render a curriculum navigation JSON as deterministic Markdown."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.scripts.learning_navigation import render_navigation_markdown
from utils.scripts.structured_io import read_json, write_text_atomic

sys.stdout.reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="渲染课程导航 Markdown")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    write_text_atomic(args.output, render_navigation_markdown(read_json(args.input)))
    print(f"已渲染：{args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

