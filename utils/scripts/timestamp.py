"""Generate project-standard timestamps and collision-safe filename timestamps."""

from __future__ import annotations

import argparse
from datetime import datetime
import sys
from typing import Iterable

sys.stdout.reconfigure(encoding="utf-8")


def iso_timestamp(value: datetime | None = None) -> str:
    """Return a local ISO 8601 timestamp without fractional seconds or offset."""
    current = value or datetime.now().astimezone().replace(tzinfo=None)
    return current.replace(microsecond=0).isoformat(timespec="seconds")


def filename_timestamp(value: datetime | None = None) -> str:
    """Return a filename-safe timestamp in YYYYMMDDTHHMMSS form."""
    return iso_timestamp(value).replace("-", "").replace(":", "")


def unique_filename_timestamp(
    existing: Iterable[str], value: datetime | None = None
) -> str:
    """Append the smallest numeric suffix needed to avoid a same-kind collision."""
    used = set(existing)
    base = filename_timestamp(value)
    if base not in used:
        return base
    suffix = 1
    while f"{base}_{suffix}" in used:
        suffix += 1
    return f"{base}_{suffix}"


def main() -> int:
    parser = argparse.ArgumentParser(description="生成项目统一格式的时间戳")
    parser.add_argument(
        "--filename",
        action="store_true",
        help="输出文件名格式：YYYYMMDDTHHMMSS",
    )
    parser.add_argument(
        "--existing",
        action="append",
        default=[],
        help="已使用的同类时间戳，可重复传入",
    )
    args = parser.parse_args()
    if args.filename:
        print(unique_filename_timestamp(args.existing))
    else:
        print(iso_timestamp())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
