"""Optional multi-turn handler (challenge-brief §7.4).

    respond(state, merchant_message) -> {action, body?, cta?, wait_seconds?, rationale}

`state` is a dict: {"conversation_id", "merchant_id", "customer_id"?, "category", "merchant",
"trigger"?, "customer"?, "history"?: [{"from": "bot"|"merchant", "body": str}]}.
Each call replays the given history into a fresh engine, so it is stateless and deterministic.
"""
from __future__ import annotations

from vera.engine import Engine
from vera.store import ContextStore


def respond(state: dict, merchant_message: str) -> dict:
    store = ContextStore()
    eng = Engine(store)
    m = state.get("merchant") or {}
    mid = state.get("merchant_id") or m.get("merchant_id")
    if state.get("category"):
        store.put("category", state["category"].get("slug", "cat"), 1, state["category"])
    if m:
        store.put("merchant", mid, 1, m)
    if state.get("customer"):
        store.put("customer", state["customer"].get("customer_id"), 1, state["customer"])
    conv_id = state.get("conversation_id") or "conv_local"
    trg = state.get("trigger")
    if trg:
        store.put("trigger", trg["id"], 1, trg)
        actions = eng.tick(None, [trg["id"]])
        if actions:   # map the engine's conversation onto the caller's id
            conv = eng.conversations.pop(actions[0]["conversation_id"])
            conv["id"] = conv_id
            eng.conversations[conv_id] = conv
    turn = 2
    for h in state.get("history") or []:
        if h.get("from") != "bot":
            eng.reply({"conversation_id": conv_id, "merchant_id": mid, "customer_id": state.get("customer_id"),
                       "from_role": h.get("from", "merchant"), "message": h.get("body", ""), "turn_number": turn})
            turn += 1
    return eng.reply({"conversation_id": conv_id, "merchant_id": mid, "customer_id": state.get("customer_id"),
                      "from_role": "merchant", "message": merchant_message, "turn_number": turn})
