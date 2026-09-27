"""HTTP contract tests (challenge-testing-brief §2, api-call-examples)."""
from tests.conftest import push

NOW = "2026-04-26T10:35:00Z"


def test_healthz_and_metadata(client):
    r = client.get("/v1/healthz")
    assert r.status_code == 200
    b = r.json()
    assert b["status"] == "ok" and b["contexts_loaded"] == {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    m = client.get("/v1/metadata").json()
    for k in ("team_name", "team_members", "model", "approach", "contact_email", "version", "submitted_at"):
        assert k in m


def test_context_versioning(client, data):
    m = data["merchants"]["m_001_drmeera_dentist_delhi"]
    r = push(client, "merchant", "m_001_drmeera_dentist_delhi", m, 1)
    assert r.status_code == 200 and r.json()["accepted"] is True and r.json()["ack_id"]
    same = push(client, "merchant", "m_001_drmeera_dentist_delhi", m, 1)
    assert same.status_code == 409 and same.json() == {"accepted": False, "reason": "stale_version", "current_version": 1}
    m2 = dict(m, performance=dict(m["performance"], views=2580))
    assert push(client, "merchant", "m_001_drmeera_dentist_delhi", m2, 2).json()["accepted"] is True
    lower = push(client, "merchant", "m_001_drmeera_dentist_delhi", m, 1)
    assert lower.status_code == 409 and lower.json()["current_version"] == 2
    import app as a
    assert a.store.get("merchant", "m_001_drmeera_dentist_delhi")["performance"]["views"] == 2580
    push(client, "merchant", "m_002_bharat_dentist_mumbai", data["merchants"]["m_002_bharat_dentist_mumbai"])
    assert client.get("/v1/healthz").json()["contexts_loaded"]["merchant"] == 2


def test_context_rejects_bad_input(client):
    assert client.post("/v1/context", json={"scope": "planet", "context_id": "x", "version": 1, "payload": {}}).json()["reason"] == "invalid_scope"
    assert client.post("/v1/context", json={"scope": "merchant", "context_id": "", "version": 1, "payload": {}}).status_code == 400
    assert client.post("/v1/context", json={"scope": "merchant", "context_id": "x", "version": "one", "payload": {}}).status_code == 400
    assert client.post("/v1/context", json={"scope": "merchant", "context_id": "x", "version": 1, "payload": []}).status_code == 400
    assert client.post("/v1/context", content=b"{not json", headers={"content-type": "application/json"}).status_code == 400
    big = {"scope": "merchant", "context_id": "x", "version": 1, "payload": {"blob": "a" * 600_000}}
    assert client.post("/v1/context", json=big).status_code == 413


def test_warmup_counts(loaded):
    assert loaded.get("/v1/healthz").json()["contexts_loaded"] == {"category": 5, "merchant": 50, "customer": 200, "trigger": 0}


def test_tick_empty_and_unknown(loaded):
    assert loaded.post("/v1/tick", json={"now": NOW, "available_triggers": []}).json() == {"actions": []}
    assert loaded.post("/v1/tick", json={"now": NOW, "available_triggers": ["trg_does_not_exist"]}).json() == {"actions": []}
    assert loaded.post("/v1/tick", json={"now": NOW}).json() == {"actions": []}


REQUIRED = ("conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id", "template_name",
            "template_params", "body", "cta", "suppression_key", "rationale")


def test_tick_action_schema_and_suppression(loaded, data):
    t = data["triggers"]["trg_001_research_digest_dentists"]
    push(loaded, "trigger", t["id"], t)
    acts = loaded.post("/v1/tick", json={"now": NOW, "available_triggers": [t["id"]]}).json()["actions"]
    assert len(acts) == 1
    a = acts[0]
    for k in REQUIRED:
        assert k in a, k
    assert a["send_as"] == "vera" and a["suppression_key"] == t["suppression_key"] and a["body"]
    # repeated trigger -> suppressed
    assert loaded.post("/v1/tick", json={"now": NOW, "available_triggers": [t["id"]]}).json()["actions"] == []
    # merchant engages, then a meaningful change (new version, new content) -> allowed again, new wording
    _reply(loaded, a["conversation_id"], a["merchant_id"], "Thanks, will read it")
    t2 = dict(t, payload=dict(t["payload"], top_item_id="d_2026W17_aligner_trend"))
    push(loaded, "trigger", t["id"], t2, version=2)
    acts2 = loaded.post("/v1/tick", json={"now": NOW, "available_triggers": [t["id"]]}).json()["actions"]
    assert len(acts2) == 1 and acts2[0]["body"] != a["body"]
    assert acts2[0]["conversation_id"] != a["conversation_id"]


def test_low_urgency_held_while_merchant_unresponsive(loaded, data):
    a = _start(loaded, data, "trg_024_perf_spike_zen")          # urgency 1, no reply yet
    t = data["triggers"]["trg_016_kids_yoga_program_drafting"]  # urgency 4 still goes through
    push(loaded, "trigger", t["id"], t)
    assert len(loaded.post("/v1/tick", json={"now": NOW, "available_triggers": [t["id"]]}).json()["actions"]) == 1
    assert a["merchant_id"] == t["merchant_id"]


def test_customer_trigger_waits_for_customer_context(client, data):
    for slug, c in data["categories"].items():
        push(client, "category", slug, c)
    push(client, "merchant", "m_001_drmeera_dentist_delhi", data["merchants"]["m_001_drmeera_dentist_delhi"])
    t = data["triggers"]["trg_003_recall_due_priya"]
    push(client, "trigger", t["id"], t)
    assert client.post("/v1/tick", json={"now": NOW, "available_triggers": [t["id"]]}).json()["actions"] == []
    push(client, "customer", "c_001_priya_for_m001", data["customers"]["c_001_priya_for_m001"])
    a = client.post("/v1/tick", json={"now": NOW, "available_triggers": [t["id"]]}).json()["actions"][0]
    assert a["send_as"] == "merchant_on_behalf" and a["customer_id"] == "c_001_priya_for_m001"
    assert "Wed 5 Nov, 6pm" in a["body"] and "₹299" in a["body"]


def test_tick_cap_and_one_per_merchant(loaded, data):
    ids = []
    for tid, t in data["triggers"].items():
        push(loaded, "trigger", tid, t)
        ids.append(tid)
    acts = loaded.post("/v1/tick", json={"now": NOW, "available_triggers": ids}).json()["actions"]
    assert 0 < len(acts) <= 20
    merchant_facing = [a["merchant_id"] for a in acts if a["send_as"] == "vera"]
    assert len(merchant_facing) == len(set(merchant_facing))
    assert len({a["conversation_id"] for a in acts}) == len(acts)
    # urgent compliance/recall items win the priority race
    assert "trg_018_supply_atorvastatin_recall" in {a["trigger_id"] for a in acts}


def _start(loaded, data, tid):
    t = data["triggers"][tid]
    push(loaded, "trigger", tid, t)
    return loaded.post("/v1/tick", json={"now": NOW, "available_triggers": [tid]}).json()["actions"][0]


def _reply(c, conv, mid, msg, turn=2, cid=None):
    return c.post("/v1/reply", json={"conversation_id": conv, "merchant_id": mid, "customer_id": cid,
                                     "from_role": "customer" if cid else "merchant", "message": msg,
                                     "received_at": "2026-04-26T10:42:00Z", "turn_number": turn}).json()


def test_reply_commit_delivers_artifact(loaded, data):
    a = _start(loaded, data, "trg_001_research_digest_dentists")
    r = _reply(loaded, a["conversation_id"], a["merchant_id"], "Yes please send it")
    assert r["action"] == "send" and "Done" in r["body"] and "CONFIRM" in r["body"]
    from vera.validate import is_qualifying
    assert not is_qualifying(r["body"])
    r2 = _reply(loaded, a["conversation_id"], a["merchant_id"], "Confirm", 3)
    assert r2["action"] == "send" and "Live" in r2["body"]
    assert _reply(loaded, a["conversation_id"], a["merchant_id"], "ok", 4)["action"] == "end"


def test_reply_auto_reply_sequence_across_conversations(loaded, data):
    mid = "m_001_drmeera_dentist_delhi"
    auto = "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly."
    acts = [_reply(loaded, f"conv_auto_{i}", mid, auto, i + 1)["action"] for i in range(1, 4)]
    assert acts == ["send", "wait", "end"]


def test_reply_opt_out_hostile_offtopic_later_decline(loaded, data):
    a = _start(loaded, data, "trg_023_competitor_opened_dentist")
    off = _reply(loaded, a["conversation_id"], a["merchant_id"], "Btw can you also help me with my GST filing?")
    assert off["action"] == "send" and "CA" in off["body"] and "?" not in off["body"].split("—")[0]
    assert _reply(loaded, a["conversation_id"], a["merchant_id"], "I'm busy, later", 3)["action"] == "wait"
    assert _reply(loaded, a["conversation_id"], a["merchant_id"], "Not interested. Stop messaging me.", 4)["action"] == "end"
    assert _reply(loaded, a["conversation_id"], a["merchant_id"], "hello?", 5)["action"] == "end"   # stays closed
    # opted-out merchant receives no further proactive sends
    t = data["triggers"]["trg_022_cde_webinar_dentists"]
    push(loaded, "trigger", t["id"], t)
    assert loaded.post("/v1/tick", json={"now": NOW, "available_triggers": [t["id"]]}).json()["actions"] == []

    b = _start(loaded, data, "trg_024_perf_spike_zen")
    h = _reply(loaded, b["conversation_id"], b["merchant_id"], "This is useless, you are wasting my time")
    assert h["action"] == "send" and "Sorry" in h["body"]
    c = _start(loaded, data, "trg_020_summer_demand_shift")
    assert _reply(loaded, c["conversation_id"], c["merchant_id"], "No thanks")["action"] == "end"


def test_reply_customer_slot_choice(loaded, data):
    t = data["triggers"]["trg_003_recall_due_priya"]
    push(loaded, "trigger", t["id"], t)
    a = loaded.post("/v1/tick", json={"now": NOW, "available_triggers": [t["id"]]}).json()["actions"][0]
    r = _reply(loaded, a["conversation_id"], a["merchant_id"], "2", cid=a["customer_id"])
    assert r["action"] == "send" and "Thu 6 Nov, 5pm" in r["body"]


def test_reply_unknown_conversation_is_handled(loaded):
    r = _reply(loaded, "conv_never_started", "m_005_pizzajunction_restaurant_delhi", "Ok lets do it. Whats next?")
    assert r["action"] == "send" and r["body"]


def test_teardown_wipes_state(loaded):
    assert loaded.post("/v1/teardown").json()["ok"] is True
    assert loaded.get("/v1/healthz").json()["contexts_loaded"]["merchant"] == 0
