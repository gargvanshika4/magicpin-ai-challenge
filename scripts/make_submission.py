#!/usr/bin/env python3
"""Generate submission.jsonl for the 30 canonical test pairs.

    python3 dataset/generate_dataset.py --seed-dir dataset --out expanded
    python3 scripts/make_submission.py --data expanded --out submission.jsonl
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot import compose  # noqa: E402


def load_dir(d: Path, key: str) -> dict:
    out = {}
    for f in sorted(d.glob("*.json")):
        obj = json.loads(f.read_text(encoding="utf-8"))
        out[obj.get(key, f.stem)] = obj
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="expanded")
    ap.add_argument("--out", default="submission.jsonl")
    a = ap.parse_args()
    root = Path(a.data)
    cats = load_dir(root / "categories", "slug")
    ms = load_dir(root / "merchants", "merchant_id")
    cs = load_dir(root / "customers", "customer_id")
    ts = load_dir(root / "triggers", "id")
    pairs = json.loads((root / "test_pairs.json").read_text(encoding="utf-8"))["pairs"]
    with open(a.out, "w", encoding="utf-8") as f:
        for p in pairs:
            t = ts[p["trigger_id"]]
            m = ms[p["merchant_id"]]
            c = cs.get(p.get("customer_id")) if p.get("customer_id") else None
            r = compose(cats.get(m["category_slug"], {}), m, t, c)
            f.write(json.dumps({"test_id": p["test_id"], **r}, ensure_ascii=False) + "\n")
    print(f"wrote {len(pairs)} lines -> {a.out}")


if __name__ == "__main__":
    main()
