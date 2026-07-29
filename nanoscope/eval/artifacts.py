"""Durable, atomic writers for evaluation evidence."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


def _atomic_replace(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_json(path: str | Path, data: Mapping[str, Any]) -> None:
    """Serialize first, then fsync and atomically replace the destination."""
    content = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_replace(Path(path), content)


def atomic_write_jsonl(path: str | Path, records: Iterable[Mapping[str, Any]]) -> None:
    """Atomically replace a JSONL artifact without exposing partial content."""
    content = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        for record in records
    )
    _atomic_replace(Path(path), content)
