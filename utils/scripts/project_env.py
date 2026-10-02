"""Load project credentials without mutating the process environment."""
from __future__ import annotations

import os
from pathlib import Path

def project_env(root: Path, name: str) -> str:
    value = os.environ.get(name)
    if value is None:
        from dotenv import dotenv_values
        value = dotenv_values(root / '.env', interpolate=False).get(name)
    value = (value or '').strip()
    if not value or value.lower() in {'todo', 'your-api-key', 'your_api_key'}:
        raise ValueError(f'请在项目根目录 .env 中填写 {name}')
    return value
