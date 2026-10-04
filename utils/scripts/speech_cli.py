"""Common speech CLI adapter and local dependency preflight (no cloud calls)."""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

WAITING = {"awaiting_context_decisions", "awaiting_semantic_decisions", "awaiting_transcript_approval", "revision_required", "preview_ready"}


class CommandStateError(ValueError):
    """A rejected command must not pause or mutate a healthy workflow."""


def transcript_next_action(status: str, *, consumer: bool = False) -> str:
    if status == "completed" and consumer:
        return "resume"
    return {
        "awaiting_semantic_decisions": "apply-decisions",
        "awaiting_transcript_approval": "approve",
        "revision_required": "apply-decisions",
        "approved": "verify",
        "verified": "verify",
        "paused_error": "resume",
        "completed": "verify",
    }.get(status, "status")


def invoke(skill_dir: Path, runner: str, argv: list[str], *, start_command: str) -> int:
    for flag in ("--run-id", "--root"):
        if flag in argv and (argv.index(flag) + 1 == len(argv) or argv[argv.index(flag) + 1].startswith("--")):
            print(json.dumps({"status": "error", "error": flag + " requires a value"}), file=sys.stderr)
            return 2
    if skill_dir.name == "run-speech-to-text" and "--run-id" in argv:
        position = argv.index("--run-id")
        run_id = argv[position + 1]
        if Path(run_id).name != run_id or run_id in {".", ".."}:
            print(json.dumps({"status": "error", "error": "Invalid run ID"}), file=sys.stderr)
            return 2
        root = Path(argv[argv.index("--root") + 1]) if "--root" in argv else skill_dir.parents[1]
        argv = [*argv[:position], "--run-dir", str(root / "outputs/run-speech-to-text/runs" / run_id), *argv[position + 2:]]
    if argv and argv[0] == "start":
        argv = [start_command, *argv[1:]]
    if argv and argv[0] == "deliver":
        if any(arg in {"--help", "-h"} for arg in argv):
            return invoke(skill_dir, runner, ["verify", *argv[1:]], start_command=start_command)
        checked = subprocess.run([sys.executable, str(skill_dir / "scripts" / runner), "status", *argv[1:]],
                                 capture_output=True, text=True, encoding="utf-8", shell=False)
        if checked.returncode:
            sys.stdout.write(checked.stdout)
            sys.stderr.write(checked.stderr)
            return 2
        state = json.loads(checked.stdout)
        if state.get("status") != "completed":
            print(json.dumps({"status": state.get("status"), "run_id": state.get("run_id"),
                              "error": "Delivery requires a completed, approved run"}, ensure_ascii=False))
            return 3
        return invoke(skill_dir, runner, ["verify", *argv[1:]], start_command=start_command)
    result = subprocess.run([sys.executable, str(skill_dir / "scripts" / runner), *argv],
                            capture_output=True, text=True, encoding="utf-8", shell=False)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if any(arg in {"--help", "-h"} for arg in argv):
        return result.returncode
    try:
        receipt = json.loads(result.stdout if result.stdout.strip() else result.stderr)
    except ValueError:
        return 2 if result.returncode else 0
    status = str(receipt.get("status", ""))
    if argv and argv[0] == "status" and not result.returncode:
        return 0
    if status == "failed_terminal":
        return 5
    if status.startswith("paused_") or status in WAITING:
        if status == "paused_configuration":
            return 6
        if status == "paused_verification":
            return 4
        if status == "paused_retryable":
            return 5
        return 3
    if result.returncode:
        if argv and argv[0] == "verify":
            return 4
        message = str(receipt.get("message", receipt.get("error", "")))
        if any(term in message for term in ("FFmpeg", "FFprobe", "ffmpeg", "ffprobe", "VOLCENGINE_API_KEY")):
            return 6
        if receipt.get("error_code") in {"runtime_error", "io_error"}:
            return 5
        return 5 if "retry" in message.lower() or "timeout" in message.lower() else 2
    return 0


def verify_installation(skill_dir: Path, environment: bool = False) -> dict:
    import importlib.util
    project = skill_dir.resolve().parents[1]
    errors = []
    for filename in ("SKILL.md", "scripts/cli.py", "references/import-provenance.json"):
        if not (skill_dir / filename).is_file():
            errors.append("Missing " + filename)
    plugin = json.loads((project / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
    if "./skills/" + skill_dir.name not in plugin["skills"]:
        errors.append("Skill is not registered")
    if environment:
        modules = ["yaml", "jsonschema", "dotenv"]
        if skill_dir.name == "run-speech-to-text":
            modules.append("websockets")
        for name in modules:
            if importlib.util.find_spec(name) is None:
                errors.append("Missing Python module " + name)
        if skill_dir.name != "convert-copy-to-transcript":
            from .media_probe import find_ffprobe
            from .ffmpeg_plan import find_ffmpeg
            for find in (find_ffmpeg, find_ffprobe):
                try:
                    find()
                except ValueError as exc:
                    errors.append(str(exc))
        if skill_dir.name == "run-text-to-speech" and importlib.util.find_spec("yaml") is not None:
            import yaml
            import wave
            from .tts_backend import check_backend, validate_voice_mapping, validate_backend_parameters, validate_backend_configuration
            try:
                config = yaml.safe_load((skill_dir / "config.yaml").read_text(encoding="utf-8"))
                if not isinstance(config, dict):
                    raise ValueError("配置必须为 YAML 对象")
                validate_backend_configuration(config)
                backend = config.get("backend", "volcengine")
                check_backend(project, backend)
                validate_backend_parameters(backend, config["audio"])
                voices = yaml.safe_load((skill_dir / "references/voices.yaml").read_text(encoding="utf-8"))
                validate_voice_mapping(voices, backend)
                if config["white_noise"]["enabled"]:
                    asset = project / config["white_noise"]["asset_path"]
                    with wave.open(str(asset), "rb") as audio:
                        if audio.getnframes() <= 0 or audio.getsampwidth() != 2:
                            errors.append("Invalid white-noise asset")
            except (OSError, ValueError, TypeError, KeyError, AttributeError, yaml.YAMLError, wave.Error) as exc:
                # Never include YAML parser excerpts, since malformed configs may contain credentials.
                errors.append(str(exc) if isinstance(exc, ValueError) else "TTS 配置、音色映射或素材无效：" + type(exc).__name__)
    return {"status": "passed" if not errors else "failed", "errors": errors,
            "skill": skill_dir.name, "cloud_verified": False}
