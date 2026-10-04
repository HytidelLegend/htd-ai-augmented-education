"""Ensure every included stage/module pair has an overview in decisions."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.scripts.structured_io import read_json, write_json

sys.stdout.reconfigure(encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = read_json(args.decisions)
    known_stages = {item["title"] for item in data["stage_overviews"]}
    known_modules = {(item["stage"], item["title"]) for item in data["module_overviews"]}
    additions = 0
    for item in data["items"]:
        if not item["include"]:
            continue
        stage, module = item["stage"], item["module"]
        if stage not in known_stages:
            data["stage_overviews"].append({
                "title": stage,
                "purpose": f"建立“{stage}”的知识框架。",
                "core_questions": [f"{stage}包含哪些核心问题？", "这些知识如何应用？"],
                "completion_criteria": [f"能说明{stage}的适用边界。", "能完成一个相关练习。"],
            })
            known_stages.add(stage)
            additions += 1
        if (stage, module) not in known_modules:
            data["module_overviews"].append({
                "stage": stage,
                "title": module,
                "purpose": f"理解“{module}”的核心内容、适用条件与限制。",
                "core_questions": [f"{module}解决什么学习问题？", "哪些结论需要进一步核验？"],
                "completion_criteria": [f"能用自己的话解释{module}。", "能完成一个相关练习或检查项。"],
            })
            known_modules.add((stage, module))
            additions += 1
    write_json(args.output, data)
    print(f"overview additions={additions}; output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
