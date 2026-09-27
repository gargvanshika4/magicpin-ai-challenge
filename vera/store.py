"""Versioned, idempotent context store.

Semantics (challenge-testing-brief §2.1 + api-call-examples 1.5/1.6):
  * (scope, context_id, version) is the idempotency key.
  * A higher version atomically replaces the stored one.
  * The same or a lower version is a no-op and reports `stale_version`
    with the current version (HTTP 409 at the API layer).

State is in-memory (the brief allows this) behind a lock. If
VERA_STATE_FILE is set, the store is snapshotted atomically after each
accepted write so a process restart mid-test doesn't lose context.
"""
from __future__ import annotations

import copy
import json
import os
import threading
from typing import Optional

SCOPES = ("category", "merchant", "customer", "trigger")


class ContextStore:
    def __init__(self, snapshot_path: Optional[str] = None):
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, dict]] = {s: {} for s in SCOPES}
        self._snapshot_path = snapshot_path
        if snapshot_path and os.path.exists(snapshot_path):
            try:
                with open(snapshot_path, encoding="utf-8") as f:
                    loaded = json.load(f)
                for s in SCOPES:
                    self._data[s] = loaded.get(s, {})
            except (OSError, ValueError):
                pass

    # ------------------------------------------------------------------ writes
    def put(self, scope: str, context_id: str, version: int, payload: dict) -> tuple[bool, int]:
        """Returns (accepted, current_version)."""
        with self._lock:
            cur = self._data[scope].get(context_id)
            if cur is not None and cur["version"] >= version:
                return False, cur["version"]
            self._data[scope][context_id] = {"version": version, "payload": copy.deepcopy(payload)}
            self._persist()
            return True, version

    def clear(self) -> None:
        with self._lock:
            self._data = {s: {} for s in SCOPES}
            if self._snapshot_path and os.path.exists(self._snapshot_path):
                try:
                    os.remove(self._snapshot_path)
                except OSError:
                    pass

    def _persist(self) -> None:
        if not self._snapshot_path:
            return
        tmp = self._snapshot_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False)
            os.replace(tmp, self._snapshot_path)
        except OSError:
            pass

    # ------------------------------------------------------------------ reads
    def get(self, scope: str, context_id: Optional[str]) -> Optional[dict]:
        if not context_id:
            return None
        with self._lock:
            rec = self._data.get(scope, {}).get(context_id)
            return rec["payload"] if rec else None

    def version(self, scope: str, context_id: str) -> Optional[int]:
        with self._lock:
            rec = self._data.get(scope, {}).get(context_id)
            return rec["version"] if rec else None

    def counts(self) -> dict:
        with self._lock:
            return {s: len(self._data[s]) for s in SCOPES}

    def category_for(self, merchant: Optional[dict]) -> Optional[dict]:
        if not merchant:
            return None
        slug = merchant.get("category_slug")
        return self.get("category", slug) if slug else None
