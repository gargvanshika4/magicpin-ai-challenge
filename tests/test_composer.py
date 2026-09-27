"""Composer quality + safety across the whole generated dataset."""
import re

import pytest

from bot import compose as bot_compose
from vera import util, validate
from vera.composer import compose
from vera.facts import Facts

SEND_AS = {"vera", "merchant_on_behalf"}
CTAS = {"binary_yes_no", "binary_confirm_cancel", "multi_choice_slot", "open_ended", "none"}
GENERIC = ["increase your sales", "we noticed an opportunity", "would you like to run a campaign",
           "i hope you're doing well", "reaching out today", "amazing deal", "flat 30% off"]


def _ctx(data, tid):
    t = data["triggers"][tid]
    m = data["merchants"][t["merchant_id"]]
    return data["categories"][m["category_slug"]], m, t, data["customers"].get(t.get("customer_id"))


def all_ids(data):
    return sorted(data["triggers"])


def test_every_trigger_composes_valid_grounded_output(data):
    for tid in all_ids(data):
        cat, m, t, c = _ctx(data, tid)
        out = compose(cat, m, t, c)
        F = Facts(cat, m, t, c)
        assert out["send_as"] in SEND_AS, tid
        assert out["cta"] in CTAS, tid
        assert out["body"] and out["rationale"] and out["suppression_key"], tid
        rep = validate.check(out["body"], F, out["cta"])
        # derived numbers are registered inside compose; recheck with a fresh Facts would miss them,
        # so only require: no URL, no taboo, no jargon, <=1 CTA question
        assert "url in body" not in rep.problems, tid
        assert not any(p.startswith("taboo") or p.startswith("internal jargon") for p in rep.problems), (tid, rep.problems)
        unquoted = re.sub(r'"[^"]*"', "", out["body"])
        assert unquoted.count("?") <= (2 if out["cta"] == "open_ended" else 1), tid
        low = out["body"].lower()
        assert not any(g in low for g in GENERIC), tid
        assert "Safe fallback" not in out["rationale"], tid


def test_numbers_are_grounded(data):
    """Every number in every message traces back to the inputs (or an explicit derivation)."""
    from vera import strategies as S
    for tid in all_ids(data):
        cat, m, t, c = _ctx(data, tid)
        F = Facts(cat, m, t, c)
        out = compose(cat, m, t, c)
        allowed = F.allowed_numbers()
        # re-run the strategy on this Facts instance to collect its derived numbers
        S.STRATEGIES.get(t["kind"], S.merchant_generic)(F) if not out["customer_scoped"] or c else None
        allowed |= F.allowed_numbers()
        bad = [n for n in util.extract_numbers(out["body"]) if n not in validate.SMALL_OK
               and not util.number_is_grounded(n, allowed)]
        assert not bad, (tid, bad, out["body"])


def test_deterministic(data):
    for tid in all_ids(data)[:40]:
        args = _ctx(data, tid)
        assert compose(*args) == compose(*args)


def test_bot_py_contract(data):
    out = bot_compose(*_ctx(data, "trg_001_research_digest_dentists"))
    assert set(out) == {"body", "cta", "send_as", "suppression_key", "rationale"}


def test_merchant_name_used(data):
    for tid in all_ids(data):
        cat, m, t, c = _ctx(data, tid)
        out = compose(cat, m, t, c)
        if out["send_as"] == "vera":
            owner = util.strip_dr(m["identity"].get("owner_first_name", ""))
            assert owner and owner in out["body"], tid


# ------------------------------------------------------------- anchor cases (decision quality)
def test_research_digest_anchors_on_cohort(data):
    b = compose(*_ctx(data, "trg_001_research_digest_dentists"))["body"]
    assert "JIDA Oct 2026" in b and "2,100" in b and "38%" in b and "124" in b and b.startswith("Dr. Meera")


def test_recall_uses_real_slots_price_and_language(data):
    o = compose(*_ctx(data, "trg_003_recall_due_priya"))
    assert o["send_as"] == "merchant_on_behalf" and o["cta"] == "multi_choice_slot"
    assert "Wed 5 Nov, 6pm" in o["body"] and "Thu 6 Nov, 5pm" in o["body"] and "₹299" in o["body"]
    assert re.search(r"\b(hai|ya|hain)\b", o["body"])          # hi-en mix honoured
    assert "fluoride" not in o["body"].lower()                  # no invented freebies


def test_perf_dip_picks_one_fix(data):
    b = compose(*_ctx(data, "trg_004_perf_dip_bharat"))["body"]
    assert "50%" in b and "12" in b and "₹299" in b
    assert "unverified" not in b.lower() and "renew" not in b.lower()   # no signal dumping


def test_ipl_saturday_contrarian(data):
    b = compose(*_ctx(data, "trg_010_ipl_match_delhi"))["body"]
    assert "DC vs MI" in b and "12%" in b and "delivery" in b.lower()


def test_competitor_no_price_war_and_computed_gap(data):
    b = compose(*_ctx(data, "trg_023_competitor_opened_dentist"))["body"]
    assert "Smile Studio" in b and "₹100" in b and "1.3 km" in b


def test_refill_cross_checks_recall_and_uses_real_offers(data):
    o = compose(*_ctx(data, "trg_019_chronic_refill_grandfather"))
    b = o["body"]
    assert "metformin" in b and "28 Apr" in b and "Senior Citizen 15% OFF" in b and "recall" in b.lower()
    assert o["cta"] == "binary_confirm_cancel" and b.startswith("Namaste")


def test_supply_alert_continues_existing_thread(data):
    b = compose(*_ctx(data, "trg_018_supply_atorvastatin_recall"))["body"]
    assert "AT2024-1102" in b and "following up" in b.lower() and "240" in b


def test_planning_intent_goes_straight_to_draft(data):
    b = compose(*_ctx(data, "trg_013_corporate_thali_planning"))["body"]
    assert "₹149" in b and "₹134" in b and not validate.is_qualifying(b)


def test_far_renewal_is_not_a_hard_sell(data):
    b = compose(*_ctx(data, "trg_092_renewal_due_m_033_anand_restaura"))["body"]
    assert "no renewal action is needed yet" in b


def test_spike_placeholder_never_frames_a_problem_as_good_news(data):
    for tid in all_ids(data):
        cat, m, t, c = _ctx(data, tid)
        if t["kind"] in ("perf_spike", "milestone_reached"):
            b = compose(cat, m, t, c)["body"].lower()
            assert not ("good news" in b and ("down" in b or "no live offer" in b or "ends in" in b)), tid
            assert "vs a peer average" not in b or "above" in b, tid


# ------------------------------------------------------------- safety edges
def test_consent_blocked_routes_to_merchant(data):
    cat, m, t, c = _ctx(data, "trg_003_recall_due_priya")
    c2 = dict(c, preferences=dict(c["preferences"], reminder_opt_in=False))
    o = compose(cat, m, t, c2)
    assert o["send_as"] == "vera" and "opted in" in o["body"] and "Priya" in o["body"]


def test_missing_customer_and_unknowns(data):
    cat, m, t, _ = _ctx(data, "trg_003_recall_due_priya")
    assert compose(cat, m, t, None)["send_as"] == "vera"
    o = compose(cat, m, dict(t, kind="brand_new_kind", scope="merchant", customer_id=None, payload={}), None)
    assert o["body"] and o["send_as"] == "vera"
    o = compose({}, m, {"kind": "perf_dip", "payload": {}}, None)            # unknown category
    assert o["body"] and "Meera" in o["body"]
    o = compose({}, {}, {}, None)                                           # nothing at all
    assert o["body"]


def test_taboo_words_are_caught():
    F = Facts({"voice": {"vocab_taboo": ["guaranteed"]}}, {}, {}, None)
    assert not validate.check("Guaranteed results! Reply YES.", F).ok
    assert not validate.check("See https://x.com. Reply YES?", F).ok
    assert not validate.check("You got 9999 calls. Reply YES?", F).ok


def test_submission_file_shape(data, tmp_path):
    import json, subprocess, sys
    from tests.conftest import ROOT, EXP
    out = tmp_path / "s.jsonl"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "make_submission.py"), "--data", str(EXP), "--out", str(out)],
                   check=True, stdout=subprocess.DEVNULL)
    lines = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 30 and [l["test_id"] for l in lines] == [f"T{i:02d}" for i in range(1, 31)]
    for l in lines:
        assert set(l) == {"test_id", "body", "cta", "send_as", "suppression_key", "rationale"}
