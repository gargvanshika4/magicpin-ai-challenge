"""Submission entry point required by challenge-brief §7.1.

    compose(category, merchant, trigger, customer) -> {body, cta, send_as, suppression_key, rationale}

Deterministic, no network, well under the 30s budget. The HTTP server
(app.py) uses the same engine.
"""
from __future__ import annotations

from typing import Optional

from vera.composer import compose as _compose


def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None) -> dict:
    out = _compose(category, merchant, trigger, customer)
    return {k: out[k] for k in ("body", "cta", "send_as", "suppression_key", "rationale")}
