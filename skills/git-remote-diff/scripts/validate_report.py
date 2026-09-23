from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="验证 git-remote-diff 的 JSON/Markdown 报告")
    parser.add_argument("--json", required=True)
    parser.add_argument("--markdown", required=True)
    args = parser.parse_args()
    data = json.loads(Path(args.json).read_text(encoding="utf-8"))
    markdown = Path(args.markdown).read_text(encoding="utf-8")
    required = ("run_id", "comparison", "differences", "remote", "local")
    missing = [key for key in required if key not in data]
    paths = [item["path"] for group in data.get("differences", {}).values() for item in group]
    errors = missing + [path for path in paths if path not in markdown]
    if "password=" in markdown.lower() or "token=" in markdown.lower() or "<redacted>" not in markdown and "http" in markdown and "@" in markdown:
        errors.append("可能包含未脱敏认证信息")
    if errors:
        raise SystemExit("报告验证失败: " + ", ".join(errors))
    print("report validation passed")


if __name__ == "__main__":
    main()
