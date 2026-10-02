from __future__ import annotations

import zipfile
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))
import format_conversion as conversion  # noqa: E402


def _make_epub(path: Path) -> None:
    container = b'''<?xml version="1.0"?><container><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>'''
    opf = b'''<?xml version="1.0"?><package><manifest><item id="x" href="x.xhtml" media-type="application/xhtml+xml"/><item id="pic" href="pic.png" media-type="image/png"/><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/></manifest><spine><itemref idref="x"/></spine></package>'''
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/x.xhtml", b'''<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body><h1>Title</h1><p>Hello <a href="https://example.com">link</a>.</p><table><tr><td>A</td><td>B</td></tr></table><p><a epub:type="noteref" href="#fn1">1</a></p><div id="fn1" class="footnote">Note</div><img src="pic.png" alt="pic"/></body></html>''')
        archive.writestr("OEBPS/pic.png", b"PNG")
        archive.writestr("OEBPS/nav.xhtml", b'''<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body><nav epub:type="toc"><ol><li><a href="x.xhtml">Title</a></li></ol></nav></body></html>'''.replace(b' epub:type', b' xmlns:epub="http://www.idpf.org/2007/ops" epub:type'))


def _make_xhtml_epub(path: Path, xhtml: bytes) -> None:
    container = b'''<?xml version="1.0"?><container><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>'''
    opf = b'''<?xml version="1.0"?><package><manifest><item id="x" href="x.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="x"/></spine></package>'''
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/x.xhtml", xhtml)


def test_validate_epub_and_pdf(tmp_path: Path) -> None:
    epub = tmp_path / "book.epub"
    _make_epub(epub)
    info = conversion._validate_epub(epub)
    assert info["spine_items"] == 1

    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.7\n1 0 obj /Type /Page >> endobj\n%%EOF\n")
    assert conversion._verify_pdf(pdf)["page_count"] == 1


def test_invalid_epub_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.epub"
    path.write_bytes(b"not an epub")
    with pytest.raises(conversion.ConversionError):
        conversion._validate_epub(path)


def test_run_pauses_when_calibre_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    epub = tmp_path / "book.epub"
    _make_epub(epub)
    monkeypatch.setattr(conversion.shutil, "which", lambda name: None)
    result = conversion.run(tmp_path, str(epub), "pdf")
    assert result["status"] == "paused_configuration"
    assert result["resume_stage"] == "prepared"


def test_request_template_and_configuration_resume_transition(tmp_path: Path) -> None:
    request = {"input_file": "book.epub", "target_format": "md", "output_file": None, "paper_size": "a4"}
    conversion.validate_json_schema(request, conversion.REQUEST_SCHEMA)
    assert {item["target"] for item in conversion.SUPPORTED_CONVERSIONS} == {"pdf", "md"}
    assert "prepared" in conversion.DEFINITION.transitions["paused_configuration"]
    assert "planning" in conversion.DEFINITION.transitions["paused_error"]


def test_init_request_cli_writes_valid_request(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    request_path = tmp_path / "request.json"
    assert conversion.main([
        "init-request", "--root", str(tmp_path), "--request-file", str(request_path),
        "--input", "book.epub", "--to", "pdf",
    ]) == 0
    assert conversion._read_request(request_path)["paper_size"] == "a4"
    capsys.readouterr()


def test_markdown_run_publishes_to_outputs_and_verifies(tmp_path: Path) -> None:
    epub = tmp_path / "book.epub"
    _make_epub(epub)
    result = conversion.run(tmp_path, str(epub), "md")
    assert result["status"] == "completed"
    output = Path(result["publication"]["path"])
    assert output.is_file() and output.parent.name == result["run_id"]
    assert "outputs" in output.parts
    text = output.read_text(encoding="utf-8")
    assert "# Title" in text and "[link](https://example.com)" in text
    assert "## 目录" in text and "- [Title](x.xhtml)" in text
    assert "A" in text and "B" in text and "Note" in text
    assert "assets/" in text and "[^fn1]" in text
    assert conversion.verify(tmp_path, result["run_id"])["sha256"] == result["publication"]["sha256"]


def test_markdown_external_output_is_allowed(tmp_path: Path) -> None:
    epub = tmp_path / "book.epub"
    _make_epub(epub)
    target = tmp_path / "exports" / "book.md"
    result = conversion.run(tmp_path, str(epub), "md", str(target))
    assert result["status"] == "completed"
    assert target.is_file()


def test_markdown_pauses_on_unparseable_xhtml(tmp_path: Path) -> None:
    epub = tmp_path / "bad.epub"
    _make_xhtml_epub(epub, b"<html><body><p>broken")
    result = conversion.run(tmp_path, str(epub), "md")
    assert result["status"] == "paused_parse"
    assert result["resume_stage"] == "extracting_content"


def test_markdown_pauses_on_image_only_xhtml(tmp_path: Path) -> None:
    epub = tmp_path / "scan.epub"
    _make_xhtml_epub(epub, b'''<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body><img src="page.png"/></body></html>''')
    result = conversion.run(tmp_path, str(epub), "md")
    assert result["status"] == "paused_scanned_content"
