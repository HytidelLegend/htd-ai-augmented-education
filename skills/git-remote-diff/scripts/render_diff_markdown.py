from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from git_remote_diff import render_markdown  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="将 git-remote-diff 的 JSON 报告渲染为 Markdown")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    Path(args.output).write_text(render_markdown(data), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()

