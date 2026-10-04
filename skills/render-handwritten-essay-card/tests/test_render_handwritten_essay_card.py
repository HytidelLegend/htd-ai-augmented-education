import json
import subprocess
import sys
from pathlib import Path

def test_start_pause_resume_and_verify(tmp_path: Path):
    root = Path(__file__).parents[3]
    cli = root / "skills" / "render-handwritten-essay-card" / "scripts" / "cli.py"
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"content": "This is a short English essay.\n\nIt has two paragraphs."}), encoding="utf-8")
    started = subprocess.run([sys.executable, str(cli), "start", "--root", str(tmp_path), "--input", str(request)], capture_output=True, text=True, encoding="utf-8")
    assert started.returncode == 3, started.stderr
    run_id = json.loads(started.stdout)["run_id"]
    delivered = subprocess.run([sys.executable, str(cli), "deliver", "--root", str(tmp_path), "--run-id", run_id, "--mode", "preview"], capture_output=True, text=True, encoding="utf-8")
    assert delivered.returncode == 3, delivered.stderr
    assert delivered.stdout.startswith("```text\n")
    assert "\n\n是否现在调用 `/imagen`" in delivered.stdout
    declined = subprocess.run([sys.executable, str(cli), "resume", "--root", str(tmp_path), "--run-id", run_id, "--decision", "decline"], capture_output=True, text=True, encoding="utf-8")
    assert declined.returncode == 0, declined.stderr
    verified = subprocess.run([sys.executable, str(cli), "verify", "--root", str(tmp_path), "--run-id", run_id], capture_output=True, text=True, encoding="utf-8")
    assert verified.returncode == 0, verified.stderr

def test_accept_copies_image_file(tmp_path: Path):
    root = Path(__file__).parents[3]
    cli = root / "skills" / "render-handwritten-essay-card" / "scripts" / "cli.py"
    request = tmp_path / "request.json"
    image = tmp_path / "generated.png"
    request.write_text(json.dumps({"content": "A valid English essay."}), encoding="utf-8")
    image.write_bytes(b"fake-image")
    started = subprocess.run([sys.executable, str(cli), "start", "--root", str(tmp_path), "--input", str(request)], capture_output=True, text=True, encoding="utf-8")
    run_id = json.loads(started.stdout)["run_id"]
    accepted = subprocess.run([sys.executable, str(cli), "resume", "--root", str(tmp_path), "--run-id", run_id, "--decision", "accept", "--image-path", str(image)], capture_output=True, text=True, encoding="utf-8")
    assert accepted.returncode == 0, accepted.stderr
    assert (tmp_path / "outputs" / "render-handwritten-essay-card" / "runs" / run_id / "generated.png").is_file()
