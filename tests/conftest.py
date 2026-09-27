import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
EXP = ROOT / "expanded"


def _ensure_expanded():
    if not (EXP / "test_pairs.json").exists():
        subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "dataset" / "generate_dataset.py"),
                        "--seed-dir", str(ROOT / "dataset"), "--out", str(EXP)], check=True,
                       stdout=subprocess.DEVNULL)


def _load(d, key):
    return {json.loads(f.read_text(encoding="utf-8"))[key]: json.loads(f.read_text(encoding="utf-8")) for f in sorted((EXP / d).glob("*.json"))}


@pytest.fixture(scope="session")
def data():
    _ensure_expanded()
    return {"categories": _load("categories", "slug"), "merchants": _load("merchants", "merchant_id"),
            "customers": _load("customers", "customer_id"), "triggers": _load("triggers", "id"),
            "pairs": json.loads((EXP / "test_pairs.json").read_text(encoding="utf-8"))["pairs"]}


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    import app as app_module
    app_module.store.clear()
    app_module.engine.reset_state()
    return TestClient(app_module.app)


def push(client, scope, cid, payload, version=1):
    return client.post("/v1/context", json={"scope": scope, "context_id": cid, "version": version,
                                             "payload": payload, "delivered_at": "2026-04-26T10:00:00Z"})


@pytest.fixture()
def loaded(client, data):
    for slug, c in data["categories"].items():
        push(client, "category", slug, c)
    for mid, m in data["merchants"].items():
        push(client, "merchant", mid, m)
    for cid, c in data["customers"].items():
        push(client, "customer", cid, c)
    return client
