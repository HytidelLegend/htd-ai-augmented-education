"""Generate and verify minimal project-relative approved-transcript references."""
from __future__ import annotations

from pathlib import Path
from jsonschema import Draft202012Validator
from .artifact_manifest import ArtifactManifestStore, relative_path
from .file_transaction import file_sha256
from .structured_io import read_json
from .run_state import write_json

SCHEMA = Path(__file__).resolve().parents[1] / "references/speech-handoff-v1.schema.json"
PRODUCERS = {"convert-copy-to-transcript", "run-speech-to-text"}


def producer_paths(root: Path, skill: str, run_id: str) -> dict[str, Path]:
    if skill not in PRODUCERS or not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
        raise ValueError("Invalid transcript producer or run ID")
    output = root / "outputs" / skill / "runs" / run_id
    output.resolve().relative_to((root / "outputs" / skill / "runs").resolve())
    base = output / "transcript" if skill == "convert-copy-to-transcript" else output
    return {"manifest": base / "manifest.json", "approved_transcript": base / "approved/transcript.txt",
            "approval_receipt": base / ("approved/approval-receipt.json" if skill == "convert-copy-to-transcript"
                                         else "approved/transcript-approval.json"),
            "state": root / "logs" / skill / "runs" / run_id / "state.json"}


def build_handoff(root: Path, skill: str, run_id: str) -> dict:
    paths = producer_paths(root, skill, run_id)
    state = read_json(paths["state"])
    manifest = read_json(paths["manifest"])
    if state["status"] != "completed" or manifest["status"] != "completed":
        raise ValueError("Producer must be completed and verified")
    ArtifactManifestStore(root=root, path=paths["manifest"]).verify_files(manifest)
    receipt = read_json(paths["approval_receipt"])
    approved_hash = file_sha256(paths["approved_transcript"])
    if receipt.get("approved_transcript_sha256", receipt.get("transcript_sha256")) != approved_hash:
        raise ValueError("Approval receipt does not match transcript")
    if not receipt.get("confirmed_by"):
        raise ValueError("Approval receipt has no confirmation source")
    result = {"schema_version": "1.0", "producer_skill": skill, "producer_run_id": run_id}
    for key in ("manifest", "approved_transcript", "approval_receipt"):
        result[key + "_path"] = relative_path(root, paths[key])
        result[key + "_sha256"] = file_sha256(paths[key])
    Draft202012Validator(read_json(SCHEMA)).validate(result)
    return result


def verify_handoff(root: Path, value: dict) -> dict:
    Draft202012Validator(read_json(SCHEMA)).validate(value)
    actual = build_handoff(root, value["producer_skill"], value["producer_run_id"])
    if actual != value:
        raise ValueError("Upstream paths or hashes changed since handoff")
    # Call the producer's full verifier, which also checks transformation/alignment.
    import subprocess
    import sys
    paths = producer_paths(root, value["producer_skill"], value["producer_run_id"])
    arguments = (["--run-id", value["producer_run_id"]] if value["producer_skill"] == "convert-copy-to-transcript"
                 else ["--run-dir", str(paths["manifest"].parent)])
    implementation_root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, str(implementation_root / "skills" / value["producer_skill"] / "scripts/cli.py"),
                             "verify", "--root", str(root), *arguments], capture_output=True, text=True,
                            encoding="utf-8", shell=False)
    if result.returncode:
        raise ValueError("Producer verification failed: " + result.stderr.strip())
    return actual


def publish_handoff(root: Path, skill: str, run_id: str, path: Path) -> dict:
    value = build_handoff(root, skill, run_id)
    write_json(path, value)
    return value
