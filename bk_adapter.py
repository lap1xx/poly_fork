#!/usr/bin/env python3
"""
bk_adapter.py - abstraction for secondary bookmakers (1win, Winline, ...).

For now this is a stub: no real WS implementation yet.
The UI already renders two BK chips; the second chip is greyed out
when config.secondary_bk.enabled == false.

When implementing a real adapter:
  1. Create a class with .start(), .stop(), .bindings (dict slug->binding),
     .live_bc(slug) returning {outcome: {src, o, c}}.
  2. Wire it into polyarb_v4.py the same way as bb_bridge.
"""

from __future__ import annotations
import pathlib
import json
from typing import Dict

HERE = pathlib.Path(__file__).parent
CONFIG_FILE = HERE / "config.json"

def _load_cfg() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

class NullAdapter:
    """Placeholder adapter: always empty."""
    name = "SecondaryBK"

    def __init__(self, name: str = "SecondaryBK"):
        self.name = name

    def is_enabled(self) -> bool:
        return False

    def bindings(self) -> Dict[str, dict]:
        return {}

    def live_bc(self, slug: str):
        return None

_GLOBAL: NullAdapter | None = None

def get_secondary() -> NullAdapter:
    """Return the secondary BK adapter. Currently always NullAdapter."""
    global _GLOBAL
    if _GLOBAL is None:
        cfg = _load_cfg()
        sec = cfg.get("secondary_bk") or {}
        name = sec.get("name") or "1win"
        _GLOBAL = NullAdapter(name=name)
    return _GLOBAL

def is_secondary_enabled() -> bool:
    cfg = _load_cfg()
    return bool((cfg.get("secondary_bk") or {}).get("enabled"))
