"""Frozen source identity preserves heading/image locators when sources move."""
from pathlib import Path
import sys
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from utils.scripts.markdown_structure import extract_markdown_structure
from utils.scripts.learning_material_backup import backup_plan, ensure_backup, verify_backup
from utils.scripts.structured_io import json_digest


def test_backup_keeps_image_and_heading_identity(tmp_path):
    source = tmp_path / 'books/book.md'; source.parent.mkdir()
    (source.parent / 'image.png').write_bytes(b'synthetic')
    source.write_text('# 标题\n![图片](image.png)\n', encoding='utf-8')
    original = extract_markdown_structure(source, root=tmp_path, preview_chars=0)
    nav = {'sources': [{'source_id': original['source_id'], 'path': original['path'], 'sha256': original['sha256']}]}
    project = tmp_path / 'project'
    plan = backup_plan(tmp_path, project, nav)
    ensure_backup(tmp_path, project, nav, tmp_path / 'logs/test', json_digest(plan))
    copy = verify_backup(tmp_path, project, nav)[original['path']]
    source.unlink()
    moved = extract_markdown_structure(copy, root=tmp_path, preview_chars=0, identity_path=original['path'])
    assert moved == original
