"""Disk cache under ~/.eeesoc/cache."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def cache_root() -> Path:
    override = os.environ.get("EEESOC_CACHE")
    if override:
        root = Path(override).expanduser()
    else:
        root = Path.home() / ".eeesoc" / "cache"
    root.mkdir(parents=True, exist_ok=True)
    return root


def cache_path(*parts: str) -> Path:
    path = cache_root()
    for part in parts[:-1]:
        path = path / part
        path.mkdir(parents=True, exist_ok=True)
    return path / parts[-1]


def read_json(path: Path) -> Any | None:
    if not path.is_file():
        return None
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.replace(path)
