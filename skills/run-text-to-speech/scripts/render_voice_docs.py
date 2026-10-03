"""Render deterministic Markdown voice tables from voices.yaml; --check is read-only."""
import argparse
import sys
from pathlib import Path
import yaml

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL.parents[1]))
sys.path.insert(0, str(SKILL.parent / "convert-copy-to-transcript/scripts"))
START = "<!-- voice-mapping:start -->"
END = "<!-- voice-mapping:end -->"


def render() -> str:
    data = yaml.safe_load((SKILL / "references/voices.yaml").read_text(encoding="utf-8"))
    rows = [START, "| 后端 | 名称 | 音色 ID | 资源 ID |", "|---|---|---|---|"]
    for backend, voices, default in [("volcengine", data["voices"], data["default_voice"]),
                                     ("edge-tts", data["edge_tts"]["voices"], data["edge_tts"]["default_voice"])]:
        for voice in voices:
            name = voice["name"] + ("（默认）" if voice["name"] == default else "")
            rows.append(f'| {backend} | {name} | {voice["speaker_id"]} | {voice.get("resource_id", "不适用")} |')
    return "\n".join([*rows, END])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for filename in ("SKILL.md", "README.md"):
        path = SKILL / filename
        original = path.read_text(encoding="utf-8")
        before, tail = original.split(START, 1)
        _, after = tail.split(END, 1)
        updated = before + render() + after
        if args.check and updated != original:
            raise SystemExit(f"音色表过期：{filename}")
        if not args.check:
            path.write_text(updated, encoding="utf-8")
    from convert_copy_to_transcript import DEFINITION
    from utils.scripts.speech_cli import transcript_next_action
    marker_start = "<!-- state-commands:start -->"
    marker_end = "<!-- state-commands:end -->"
    rows = [marker_start, "| 当前状态 | 下一步命令 |", "|---|---|"]
    for status in sorted(DEFINITION.statuses):
        action = transcript_next_action(status)
        if action != "status":
            rows.append(f"| `{status}` | `{action}` |")
    block = "\n".join([*rows, marker_end])
    path = SKILL.parent / "convert-copy-to-transcript/SKILL.md"
    original = path.read_text(encoding="utf-8")
    if marker_start in original:
        before, tail = original.split(marker_start, 1)
        _, after = tail.split(marker_end, 1)
        updated = before + block + after
    else:
        updated = original.rstrip() + "\n\n" + block + "\n"
    if args.check and updated != original:
        raise SystemExit("转换状态命令表过期")
    if not args.check:
        path.write_text(updated, encoding="utf-8")


if __name__ == "__main__":
    main()
