"""Export one validated ASR run as a reviewable Markdown transcript."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

from run_speech_to_text import verify_run, _paths, SUPPORTED_EXTENSIONS


sys.stdout.reconfigure(encoding="utf-8")


def timecode(milliseconds: int) -> str:
    seconds = milliseconds // 1000
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def main() -> int:
    parser = argparse.ArgumentParser(description="将已校验的识别结果导出为 Markdown 草稿")
    parser.add_argument("--root", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    root = Path(args.root).resolve()
    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = root / run_dir
    run_dir = run_dir.resolve()
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = root / output_dir
    output_dir = output_dir.resolve()
    output_dir.relative_to((root / "outputs").resolve())

    verification = verify_run(root, str(run_dir))
    paths = _paths(run_dir)
    state = json.loads(paths["state"].read_text(encoding="utf-8"))
    metadata = json.loads(paths["metadata"].read_text(encoding="utf-8"))
    source = Path(metadata["source_path"])
    if source.suffix.lower() not in SUPPORTED_EXTENSIONS or not source.is_file():
        raise ValueError("来源音视频不存在或格式不支持")
    approved = state["status"] == "completed"
    if state["status"] not in {"awaiting_transcript_approval", "completed"}:
        raise ValueError(f"运行状态不允许导出：{state['status']}")
    subdir = "approved" if approved else "generated"
    transcript = (run_dir / subdir / "transcript.txt").read_text(encoding="utf-8").strip()
    timestamps = json.loads((run_dir / subdir / "full.timestamps.json").read_text(encoding="utf-8"))
    sentences = timestamps["sentences"]
    transcript_lines = transcript.splitlines()
    if len(transcript_lines) != len(sentences):
        raise ValueError("句级数量与逐字稿行数不一致")
    for index, (line, sentence) in enumerate(zip(transcript_lines, sentences), start=1):
        if re.sub(r"\s+", "", line) != re.sub(r"\s+", "", sentence["text"]):
            raise ValueError(f"第 {index} 句与逐字稿不一致")

    title = source.stem
    status = "已确认" if approved else "待人工确认"
    relative_source = Path(os.path.relpath(source, output_dir)).as_posix()
    source_link = quote(relative_source, safe="/.")
    lines = [
        f"# {title}",
        "",
        f"> 转写状态：{status}。识别结果可能有错字，请对照音频检查。",
        f"> 来源音频：[{source.name}](<{source_link}>)",
        f"> 运行 ID：`{state['run_id']}`",
        "",
        "## 逐字稿",
        "",
    ]
    lines.extend(
        f"[{timecode(sentence['start_ms'])}] {line}"
        for sentence, line in zip(sentences, transcript_lines)
    )
    output = output_dir / f"{title}.md"
    output_dir.mkdir(parents=True, exist_ok=True)
    content = "\n".join(lines) + "\n"
    if output.exists() and output.read_text(encoding="utf-8") != content:
        raise ValueError(f"目标文件已存在且内容不同：{output}")
    if not output.exists():
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
    print(json.dumps({**verification, "output": str(output), "transcript_status": status, "sentences": len(sentences)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
