"""Probe current site selectors in a visible browser without saving page content."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from utils.scripts.browser_checkpoint import visible_browser
from adapters import SITES, collect_site


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--word", default="bank")
    args = parser.parse_args()
    results = []
    with visible_browser() as browser:
        context = browser.new_context()
        page = context.new_page()
        for site in SITES:
            try:
                result = collect_site(page, site, args.word)
                results.append({"site": site, "coverage": result["coverage"],
                                "senses": len(result.get("senseCandidates", [])),
                                "translatedExamples": sum(bool(candidate.get("exampleTranslationZh"))
                                                          for candidate in result.get("senseCandidates", [])),
                                "pronunciations": len(result.get("pronunciationCandidates", [])),
                                "inflections": len(result.get("inflectionCandidates", [])),
                                "derivativeCandidates": len(result.get("derivativeCandidates", [])),
                                "forms": [candidate["form"] for candidate in result.get("inflectionCandidates", [])[:8]],
                                "externalCandidates": len(result.get("externalCandidates", [])),
                                "truncatedSelectors": result.get("truncatedSelectors", []),
                                "previewLimitedSelectors": len(result.get("previewLimits", []))})
            except Exception as exc:
                results.append({"site": site, "coverage": "failed", "error": str(exc)[:160]})
        context.close()
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(result["coverage"] == "extracted" for result in results) else 3


if __name__ == "__main__":
    raise SystemExit(main())
