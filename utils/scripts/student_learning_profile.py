"""Parse, validate and render reusable student learning profiles."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.scripts.markdown_structure import sha256_file, split_frontmatter
from utils.scripts.structured_io import read_json, validate_json_schema, write_text_atomic

sys.stdout.reconfigure(encoding="utf-8")


SECTION_FIELDS = (
    ("当前阶段", "current_stage"),
    ("首要学习目标", "primary_goals"),
    ("次要学习目标", "secondary_goals"),
    ("暂不关注", "excluded_goals"),
    ("当前项目", "current_projects"),
    ("实践经验", "experience"),
    ("现实约束", "constraints"),
    ("优先学习领域", "priority_topics"),
    ("延后学习领域", "deferred_topics"),
    ("学习与输出偏好", "learning_preferences"),
)


def render_student_profile(profile: dict[str, Any]) -> str:
    frontmatter = yaml.safe_dump(profile, allow_unicode=True, sort_keys=False).strip()
    lines = ["---", frontmatter, "---", "", f"# {profile['title']}", ""]
    for heading, field in SECTION_FIELDS:
        lines.extend([f"## {heading}", ""])
        value = profile.get(field, [])
        if isinstance(value, str):
            lines.extend([value, ""])
        else:
            lines.extend([*(f"- {item}" for item in value), ""])
    lines.extend(["## 知识掌握情况", ""])
    for level, label in (("clear", "清楚"), ("medium", "中等"), ("unclear", "模糊或不会应用")):
        lines.extend([f"### {label}", ""])
        lines.extend(f"- {item}" for item in profile.get("knowledge", {}).get(level, []))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def parse_student_profile(path: Path, schema_path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8-sig")
    profile, _ = split_frontmatter(raw)
    validate_json_schema(profile, schema_path)
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "profile": profile,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="渲染或校验学生学习档案")
    subparsers = parser.add_subparsers(dest="command", required=True)
    render = subparsers.add_parser("render")
    render.add_argument("--input", type=Path, required=True)
    render.add_argument("--schema", type=Path, required=True)
    render.add_argument("--output", type=Path, required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--file", type=Path, required=True)
    validate.add_argument("--schema", type=Path, required=True)
    validate.add_argument("--rendered-output", type=Path)
    args = parser.parse_args(argv)
    if args.command == "render":
        profile = read_json(args.input)
        validate_json_schema(profile, args.schema)
        write_text_atomic(args.output, render_student_profile(profile))
        print(f"已生成：{args.output}")
    else:
        snapshot = parse_student_profile(args.file, args.schema)
        if args.rendered_output:
            write_text_atomic(args.rendered_output, render_student_profile(snapshot["profile"]))
        print(f"校验通过：{snapshot['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
