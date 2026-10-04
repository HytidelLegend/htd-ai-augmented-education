"""Shallow study copies with local-reference relocation; originals remain byte exact."""
import hashlib
import html
import os
import re
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit


def material_name(path):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', Path(path).stem).strip(' .')[:100] or '材料'
    if name.upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        name = '_' + name
    return name


def display_mapping(files, main, base):
    parent = Path(main).parent
    mapping = {}
    for file in files:
        original = Path(file['original_path'])
        if original.is_relative_to(parent): target = base / original.relative_to(parent)
        else: target = base / '_resources' / (hashlib.sha256(original.as_posix().encode()).hexdigest()[:12] + '-' + original.name)
        mapping[file['original_path']] = target.as_posix()
    return mapping


def display_bytes(root, record, mapping):
    original = record['original_path']; data = (root / original).read_bytes()
    if Path(original).suffix.lower() not in ('.md', '.markdown'): return data
    text = data.decode('utf-8-sig')
    from .learning_material_backup import markdown_references
    used = set(markdown_references(text))
    def relocate(raw):
        value = html.unescape(re.sub(r'\\([^\w\s])', r'\1', raw))
        if value not in used: return raw
        url = urlsplit(value)
        if url.scheme or url.netloc or not url.path: return raw
        target = (root / Path(original).parent / unquote(url.path)).resolve().relative_to(root.resolve()).as_posix()
        if target not in mapping: raise ValueError('展示材料引用未登记')
        relative = Path(os.path.relpath(mapping[target], Path(mapping[original]).parent)).as_posix()
        return urlunsplit(('', '', quote(relative, safe='/._-~'), url.query, url.fragment))
    # Hide code/comments before matching actual link syntax; restore them byte-for-byte.
    protected = []
    def hide(match):
        protected.append(match[0]); return f'\x00{len(protected)-1}\x00'
    # Use the same container/indent rules as resource discovery, including
    # quoted/list fences and closing fences longer than the opening marker.
    lines = []; fence = None; list_indent = None
    for original_line in text.splitlines(keepends=True):
        line = re.sub(r'^(?: {0,3}> ?)+', '', original_line.rstrip('\r\n'))
        item = re.match(r'^( *)(?:[-+*]|\d+[.)]) +', line)
        indent = len(line) - len(line.lstrip(' '))
        code = False
        if not fence:
            if item:
                list_indent = len(item[0]); line = line[len(item[0]):]
            elif line.strip() and list_indent is not None and indent < list_indent:
                list_indent = None
            elif line.strip() and indent >= (list_indent or 0) + 4:
                code = True
            elif list_indent is not None and indent >= list_indent:
                line = line[list_indent:]
        elif list_indent is not None and indent >= list_indent:
            line = line[list_indent:]
        marker = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        if fence:
            code = True
            if re.fullmatch(r'\s{0,3}' + re.escape(fence[0]) + '{' + str(fence[1]) + r',}\s*', line):
                fence = None
        elif marker and not code:
            fence = (marker[1][0], len(marker[1])); code = True
        lines.append(hide([original_line]) if code else original_line)
    text = ''.join(lines)
    text = re.sub(r'<!--.*?-->|(`+).*?\1(?!`)', hide, text, flags=re.S)
    # Match each known used destination only in a destination position.
    for raw in sorted(used, key=len, reverse=True):
        escaped_variants = {raw, html.escape(raw, quote=True), re.sub(r'([() ])', r'\\\1', raw)}
        for variant in escaped_variants:
            rx = r'(\]\(\s*<?|^\s{0,3}\[[^\]\n]+\]:\s*<?|(?:src|href|poster)\s*=\s*[\"\x27]|!?\[\[)' + re.escape(variant) + r'(?=[>)\s\"\x27\]|])'
            text = re.sub(rx, lambda m: m[1] + relocate(variant), text, flags=re.M)
    def srcset(match):
        parts = []
        for part in match[2].split(','):
            token = re.match(r'(\s*)(\S+)(.*)', part)
            parts.append(token[1] + relocate(token[2]) + token[3] if token else part)
        return match[1] + ','.join(parts) + match[3]
    text = re.sub(r'(srcset\s*=\s*[\"\x27])([^\"\x27]*)([\"\x27])', srcset, text, flags=re.I)
    text = re.sub(r'\x00(\d+)\x00', lambda m: protected[int(m[1])], text)
    # Preserve encoding and newlines unless a reference actually changed.
    if text == data.decode('utf-8-sig'): return data
    return (b'\xef\xbb\xbf' if data.startswith(b'\xef\xbb\xbf') else b'') + text.encode('utf-8')
