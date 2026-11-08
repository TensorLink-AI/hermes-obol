"""Backend for Obol's Desktop chip, mounted at /api/plugins/obol/.

Read-only: serves the snapshot the plugin writes to its own data directory, so the chip never runs a forecast or
touches the ledger from the gateway's web server.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter

from hermes_constants import get_hermes_home

router = APIRouter()
EMPTY = {"state": "none", "chip": "Obol", "lines": ["No usage recorded yet. Plan limits appear after your first request. On API keys? /obol budget 20 week"]}


def snapshot(home: Path) -> dict:
    root = Path(home) / "plugin-data"
    found = [p for p in [root / "obol" / "desktop.json", *root.glob("agent-plugin-obol-*/desktop.json")] if p.is_file()]
    if not found:
        return EMPTY
    try:
        return json.loads(max(found, key=lambda p: p.stat().st_mtime).read_text())
    except (OSError, ValueError):
        return EMPTY


@router.get("/status")
async def status():
    return snapshot(get_hermes_home())
