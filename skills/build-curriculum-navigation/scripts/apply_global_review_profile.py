"""Apply compact global review updates to a navigation decision template."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.scripts.structured_io import read_json, write_json

sys.stdout.reconfigure(encoding="utf-8")


def append_unique(values: list[Any], additions: list[Any]) -> list[Any]:
    result = list(values)
    for item in additions:
        if item not in result:
            result.append(item)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    data = read_json(args.base)
    profile = read_json(args.profile)
    metadata = {item["source_id"]: item for item in profile.get("source_metadata", [])}
    data["source_metadata"] = [metadata.get(item["source_id"], item) for item in data["source_metadata"]]
    data["source_relationships"] = append_unique(data["source_relationships"], profile.get("source_relationships", []))
    data["coverage_gaps"] = append_unique(data["coverage_gaps"], profile.get("coverage_gaps", []))
    data["deferred_items"] = append_unique(data["deferred_items"], profile.get("deferred_items", []))
    data["student_analysis"]["curriculum_impacts"] = append_unique(
        data["student_analysis"]["curriculum_impacts"], profile.get("curriculum_impacts", [])
    )
    write_json(args.output, data)
    print(f"global review profile applied: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
