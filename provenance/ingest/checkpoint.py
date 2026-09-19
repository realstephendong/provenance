"""Ingest checkpoint state: {channel_name: {"last_ts": float}}."""

from __future__ import annotations

import json
from pathlib import Path


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except (json.JSONDecodeError, OSError):
        # A corrupt checkpoint should cost a full re-ingest, not a crash.
        print(f"  ! unreadable checkpoint at {path}, treating as empty")
        return {}


def save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))
