"""Stateful orchestration: which triggers to act on each tick, and how to
answer each merchant/customer reply.

All decision logic is deterministic given (stored contexts, request body,
prior requests). Infrastructure timestamps are kept out of decisions.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import replies as R
from . import util, validate
from .composer import compose
from .facts import Facts
from .store import ContextStore

MAX_ACTIONS_PER_TICK = 20
MAX_BOT_TURNS_PER_CONV = 5
KIND_BONUS = {"supply_alert": 6, "regulation_change": 5, "renewal_due": 3, "active_planning_intent": 4,
              "chronic_refill_due": 4, "recall_due": 3, "appointment_tomorrow": 4, "perf_dip": 2,
              "ipl_match_today": 3, "competitor_opened": 2}


def _fingerprint(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


class Engine:
    def __init__(self, store: ContextStore):
        self.store = store
        self._lock = threading.RLock()
        self.reset_state()

    def reset_state(self):
        with getattr(self, "_lock", threading.RLock()):
            self.conversations: dict[str, dict] = {}
            self.sent_keys: dict[str, str] = {}          # suppression_key -> trigger fingerprint
            self.merchant_state: dict[str, dict] = {}    # opted_out, muted, wait_until, auto counts, last_conv

    def _mstate(self, mid: str) -> dict:
        return self.merchant_state.setdefault(mid or "_", {"opted_out": False, "muted": False, "wait_until": None,
                                                           "auto_counts": {}, "last_conv": None,
                                                           "auto_nudged": False})

    # ================================================================== TICK
    def tick(self, now: Optional[str], available: list) -> list[dict]:
        now_dt = util.parse_dt(now)
        with self._lock:
            candidates = []
            seen_ids = set()
            for tid in available or []:
                if not isinstance(tid, str) or tid in seen_ids:
                    continue
                seen_ids.add(tid)
                plan = self._plan(tid, now_dt)
                if plan:
                    candidates.append(plan)
            # priority: urgency + kind + engagement; deterministic tie-break on trigger id
            candidates.sort(key=lambda p: (-p["priority"], p["trigger_id"]))
            actions, used_merchants, used_customers, used_keys = [], set(), set(), set()
            for p in candidates:
                if len(actions) >= MAX_ACTIONS_PER_TICK:
                    break
                key = p["suppression_key"]
                if key in used_keys:
                    continue
                if p["customer_id"]:
                    if p["customer_id"] in used_customers:
                        continue
                elif p["merchant_id"] in used_merchants:
                    continue          # one merchant-facing message per merchant per tick (restraint)
                used_keys.add(key)
                (used_customers.add(p["customer_id"]) if p["customer_id"] else used_merchants.add(p["merchant_id"]))
                actions.append(self._commit(p))
            return actions

    def _plan(self, tid: str, now_dt) -> Optional[dict]:
        trg = self.store.get("trigger", tid)
        if not trg:
            return None
        mid = trg.get("merchant_id") or (trg.get("payload") or {}).get("merchant_id")
        merchant = self.store.get("merchant", mid)
        if not merchant:
            return None
        category = self.store.category_for(merchant) or {"slug": merchant.get("category_slug")}
        ms = self._mstate(mid)
        if ms["opted_out"] or ms["muted"]:
            return None
        if ms["wait_until"] and now_dt and now_dt < ms["wait_until"]:
            return None
        cid = trg.get("customer_id")
        customer = self.store.get("customer", cid) if cid else None
        if cid and not customer:
            return None                               # wait until the customer context arrives
        key_fp = _fingerprint([trg, self.store.version("trigger", tid)])
        # suppression: same trigger content already sent under this key -> skip
        sk = trg.get("suppression_key") or f"{trg.get('kind')}:{mid}:{cid or ''}"
        if self.sent_keys.get(sk) == key_fp:
            return None
        out = compose(category, merchant, trg, customer)
        # restraint: 3+ unanswered Vera nudges -> only urgent merchant-facing sends
        F = Facts(category, merchant, trg, customer)
        if out["send_as"] == "vera" and (trg.get("urgency") or 0) < 4:
            if F.unanswered_vera_streak() >= 3:
                return None
        if out["send_as"] == "vera" and (trg.get("urgency") or 0) < 3 and ms.get("unanswered", 0) >= 1:
            return None                               # already nudged this session with no reply: hold low-urgency sends
        prio = (trg.get("urgency") or 1) * 10 + KIND_BONUS.get(trg.get("kind"), 0)
        prio += 3 if F.merchant_engaged_recently() else 0
        prio -= 4 if "opt-in" in out["body"] else 0
        return {"trigger_id": tid, "trigger": trg, "merchant_id": mid,
                "customer_id": cid if out["customer_scoped"] else None, "compose": out,
                "suppression_key": out["suppression_key"], "fingerprint": key_fp, "priority": prio}

    def _commit(self, p: dict) -> dict:
        out, trg = p["compose"], p["trigger"]
        conv_id = self._new_conv_id(p)
        self.sent_keys[p["suppression_key"]] = p["fingerprint"]
        if trg.get("suppression_key"):
            self.sent_keys[trg["suppression_key"]] = p["fingerprint"]
        self.conversations[conv_id] = {
            "id": conv_id, "merchant_id": p["merchant_id"], "customer_id": p["customer_id"],
            "trigger_id": p["trigger_id"], "kind": trg.get("kind"), "topic": out["topic"],
            "followup": out.get("followup") or {}, "send_as": out["send_as"], "stage": "pitched",
            "bot_bodies": [out["body"]], "turns": [{"from": "bot", "body": out["body"]}], "state": "open",
            "auto_count": 0, "cta": out["cta"],
        }
        ms = self._mstate(p["merchant_id"])
        ms["last_conv"] = conv_id
        if out["send_as"] == "vera":
            ms["unanswered"] = ms.get("unanswered", 0) + 1
        return {
            "conversation_id": conv_id, "merchant_id": p["merchant_id"], "customer_id": p["customer_id"],
            "send_as": out["send_as"], "trigger_id": p["trigger_id"], "template_name": out["template_name"],
            "template_params": out["template_params"], "body": out["body"], "cta": out["cta"],
            "suppression_key": out["suppression_key"], "rationale": out["rationale"],
        }

    def _new_conv_id(self, p: dict) -> str:
        who = p["customer_id"] or p["merchant_id"]
        base = f"conv_{util.slug(who, 28)}_{util.slug(p['trigger']['kind'] or 'msg', 24)}"
        cid, n = base, 2
        while cid in self.conversations:
            cid, n = f"{base}_{n}", n + 1
        return cid

    # ================================================================== REPLY
    def reply(self, body: dict) -> dict:
        with self._lock:
            conv_id = str(body.get("conversation_id") or "")
            msg = str(body.get("message") or "")
            mid = body.get("merchant_id")
            conv = self.conversations.get(conv_id)
            if conv is None:
                conv = self._adopt_conversation(conv_id, mid, body.get("customer_id"))
            mid = conv["merchant_id"] or mid
            conv["turns"].append({"from": body.get("from_role") or "merchant", "body": msg})
            if not R.is_auto_reply(msg):
                self._mstate(mid)["unanswered"] = 0
            if conv["state"] == "ended":
                return {"action": "end", "rationale": "Conversation already closed (opt-out or completion); staying silent."}
            decision = self._decide(conv, mid, msg, body)
            if decision["action"] == "send":
                decision["body"] = self._dedupe(conv, decision["body"])
                conv["bot_bodies"].append(decision["body"])
                conv["turns"].append({"from": "bot", "body": decision["body"]})
                if len(conv["bot_bodies"]) > MAX_BOT_TURNS_PER_CONV:
                    conv["state"] = "ended"
                    return {"action": "end", "rationale": "Turn budget for this conversation reached; closing gracefully."}
            elif decision["action"] == "end":
                conv["state"] = "ended"
            elif decision["action"] == "wait":
                recv = util.parse_dt(body.get("received_at"))
                if recv:
                    self._mstate(mid)["wait_until"] = recv + timedelta(seconds=decision["wait_seconds"])
            return decision

    def _adopt_conversation(self, conv_id: str, mid: Optional[str], cid: Optional[str]) -> dict:
        """Reply on a conversation we didn't start (or lost): anchor it to the
        merchant's most recent thread, else to their stored history."""
        ms = self._mstate(mid)
        src = self.conversations.get(ms.get("last_conv")) if ms.get("last_conv") else None
        merchant = self.store.get("merchant", mid) or {}
        category = self.store.category_for(merchant) or {}
        if src:
            followup, topic, kind = dict(src["followup"]), src["topic"], src["kind"]
        else:
            F = Facts(category, merchant, {}, None)
            last_m = F.last_from("merchant")
            topic = ""
            if last_m:
                topic = R.norm(last_m.get("body", ""))
                topic = topic.split(",", 1)[-1].strip() if "," in topic else topic
                topic = topic.replace("focus on", "").strip(" .")
            from .strategies import _post_artifact
            headline = f"{topic} — ask us this week" if topic else "open this week for walk-ins and bookings"
            followup = {"label": "Google post draft", "artifact": _post_artifact(F, headline) if merchant else "",
                        "confirm": "Reply CONFIRM and I'll publish it."}
            kind = "merchant_followup"
        conv = {"id": conv_id, "merchant_id": mid, "customer_id": cid, "trigger_id": None, "kind": kind,
                "topic": topic or "your listing", "followup": followup, "send_as": "vera", "stage": "pitched",
                "bot_bodies": [], "turns": [], "state": "open", "auto_count": 0, "cta": "binary_yes_no"}
        self.conversations[conv_id] = conv
        return conv

    # ------------------------------------------------------------------ policy
    def _names(self, conv: dict):
        merchant = self.store.get("merchant", conv["merchant_id"]) or {}
        category = self.store.category_for(merchant) or {}
        customer = self.store.get("customer", conv["customer_id"]) if conv.get("customer_id") else None
        return Facts(category, merchant, {}, customer)

    def _decide(self, conv: dict, mid: str, msg: str, body: dict) -> dict:
        label = R.classify(msg)
        ms = self._mstate(mid)
        norm = R.norm(msg)
        # repeated identical text from the same merchant = auto-reply even without canned phrasing
        if label != "auto_reply" and norm and ms["auto_counts"].get(norm, 0) >= 1 and len(norm) > 25:
            label = "auto_reply"
        F = self._names(conv)
        mix = R.is_hinglish(msg)
        is_customer = conv.get("send_as") == "merchant_on_behalf"
        name = (F.customer_names()[0] if is_customer else F.salutation) if F.merchant else ""
        fu = conv.get("followup") or {}

        if label == "auto_reply":
            ms["auto_counts"][norm] = ms["auto_counts"].get(norm, 0) + 1
            conv["auto_count"] += 1
            n = max(conv["auto_count"], ms["auto_counts"][norm])
            if n == 1 and not ms["auto_nudged"]:
                ms["auto_nudged"] = True
                what = fu.get("label") or conv.get("topic") or "this"
                who = f"{name}, " if name else ""
                txt = (f"Lagta hai yeh auto-reply hai 🙂 {who}jab aap dekhein, bas YES reply kar dijiye — {what} main ready rakhungi."
                       if mix else f"Looks like an auto-reply 🙂 {who}when you see this, just reply YES and I'll have the {what} ready.")
                return {"action": "send", "body": txt, "cta": "binary_yes_no",
                        "rationale": "Detected WhatsApp Business auto-reply; one short flag for the owner, no further pitch."}
            if n <= 2:
                return {"action": "wait", "wait_seconds": 14400,
                        "rationale": "Same auto-reply again — owner isn't at the phone. Backing off 4 hours."}
            ms["muted"] = True
            return {"action": "end", "rationale": f"Auto-reply {n}x with no human response; closing to avoid wasted turns."}

        if label == "opt_out":
            ms["opted_out"] = True
            return {"action": "end",
                    "rationale": "Merchant asked to stop / not interested. Closing and suppressing all proactive sends to them."}

        if label == "hostile":
            if conv.get("apologized"):
                ms["opted_out"] = True
                return {"action": "end", "rationale": "Continued frustration after an apology; closing without further messages."}
            conv["apologized"] = True
            ms["muted"] = True
            txt = ("Sorry for the trouble — main yeh messages rok rahi hoon. Kabhi zarurat ho to bas 'Hi Vera' likh dijiye. 🙏"
                   if mix else "Sorry for the bother — I'll pause these messages. If you ever want help with your listing, just type 'Hi Vera'. 🙏")
            return {"action": "send", "body": txt, "cta": "none",
                    "rationale": "Merchant frustrated: one-line apology + opt-out path; proactive sends paused."}

        if label == "off_topic":
            who = R.off_topic_owner(msg)
            thing = "that" if not who else {"your CA": "GST and tax filing", "your bank": "loans and banking",
                                             "a lawyer": "legal matters"}.get(who, "that")
            pending = fu.get("label")
            back = (f" Meanwhile, the {pending} is ready whenever you are — reply YES and I'll send it."
                    if pending and conv["stage"] == "pitched" else
                    " I can keep helping with your Google listing, offers and customer messages.")
            if conv.get("apologized"):
                ms["muted"] = True
                back = " Happy to help with your listing whenever you want — just say 'Hi Vera'."
            txt = f"{thing[0].upper() + thing[1:]} is outside what I can help with — {who or 'someone else'} is the right person for it.{back}"
            return {"action": "send", "body": txt, "cta": "binary_yes_no" if "YES" in back else "none",
                    "rationale": "Out-of-scope request politely declined in one line; redirected to the open thread."}

        if label == "later":
            secs = R.wait_seconds_for(msg)
            return {"action": "wait", "wait_seconds": secs,
                    "rationale": f"Merchant asked for time; backing off {secs // 60} minutes."}

        if label == "decline":
            return {"action": "end", "rationale": "Merchant declined this suggestion; closing this thread without pushing."}

        if label in ("commit", "question", "info"):
            if ms.get("muted"):
                ms["muted"] = False                    # merchant re-engaged on their own
            return self._advance(conv, F, msg, label, mix, is_customer)

        return {"action": "end", "rationale": "No productive next step."}

    def _advance(self, conv: dict, F: Facts, msg: str, label: str, mix: bool, is_customer: bool) -> dict:
        fu = conv.get("followup") or {}
        stage = conv["stage"]
        if is_customer:
            return self._advance_customer(conv, F, msg, label)

        # curious-ask answers: build the artifact from what the merchant said
        if conv.get("kind") in ("curious_ask_due", "scheduled_recurring") and label in ("info", "commit", "question") and stage == "pitched":
            svc = R.extract_service(msg, F.active_offer_titles + [o.get("title", "") for o in F.category.get("offer_catalog", []) if isinstance(o, dict)])
            if svc:
                from .strategies import _post_artifact
                fu = dict(fu, label="Google post draft",
                          artifact=_post_artifact(F, f"{svc} — ask us for today's price"),
                          confirm="Reply CONFIRM and I'll publish it, plus save a matching WhatsApp price reply.")
                conv["followup"] = fu

        if label == "question" and re.search(R.PRICE_Q, R.norm(msg)) and stage == "pitched":
            amt = None
            trg = self.store.get("trigger", conv.get("trigger_id")) if conv.get("trigger_id") else None
            if trg:
                amt = (trg.get("payload") or {}).get("renewal_amount")
            price = f"The renewal is {util.inr(amt)}. " if amt else "Nothing goes live or gets charged without your OK. "
            art = fu.get("artifact")
            txt = price + (f"Here's exactly what I'd set up:\n\n{art}\n\n" if art else "") + (fu.get("confirm") or "Reply CONFIRM to go ahead.")
            conv["stage"] = "delivered"
            return {"action": "send", "body": txt, "cta": "binary_confirm_cancel",
                    "rationale": "Answered the cost question directly from context, then showed the concrete artifact."}

        if stage == "pitched":
            art = fu.get("artifact")
            label_txt = fu.get("label") or "draft"
            conv["stage"] = "delivered"
            if art:
                head = "Done ✅ Here's the " + label_txt + ":" if not mix else f"Ho gaya ✅ Here's the {label_txt}:"
                txt = f"{head}\n\n{art}\n\n{fu.get('confirm') or 'Reply CONFIRM and it goes live.'}"
            else:
                txt = f"Done ✅ I've started on the {label_txt}. Reply CONFIRM and I'll proceed."
            return {"action": "send", "body": txt, "cta": "binary_confirm_cancel",
                    "rationale": "Merchant engaged — switched straight to action mode and delivered the drafted artifact; "
                                 "no further qualifying questions."}
        if stage == "delivered":
            if label == "commit":
                conv["stage"] = "confirmed"
                txt = (f"Live ✅ The {fu.get('label') or 'update'} is done for {F.biz}. "
                       f"I'll send you a short note on how it performs next week.")
                return {"action": "send", "body": txt, "cta": "none",
                        "rationale": "Merchant confirmed; executed and closed the loop with a follow-up promise."}
            txt = ("Happy to tweak it — tell me the one change you want and I'll send the updated version right here."
                   if label != "question" else
                   "Good question — nothing goes live without your OK, and you can edit any line. "
                   "Tell me the one change you want, or reply CONFIRM to go ahead as is.")
            return {"action": "send", "body": txt, "cta": "open_ended",
                    "rationale": "Merchant responded to the draft without confirming; invited one edit, no new pitch."}
        return {"action": "end", "rationale": "Task completed and confirmed; nothing further to add right now."}

    def _advance_customer(self, conv: dict, F: Facts, msg: str, label: str) -> dict:
        t = R.norm(msg)
        trg = self.store.get("trigger", conv.get("trigger_id")) if conv.get("trigger_id") else {}
        tp = (trg or {}).get("payload") or {}
        slots = [s.get("label") for s in (tp.get("available_slots") or tp.get("next_session_options") or []) if isinstance(s, dict)]
        hi = F.customer_lang != "en"
        if conv["stage"] == "confirmed":
            return {"action": "end", "rationale": "Booking already confirmed; nothing more to send."}
        choice = None
        if slots:
            if re.search(r"^\s*1\b|first|pehla", t):
                choice = slots[0]
            elif len(slots) > 1 and re.search(r"^\s*2\b|second|dusra", t):
                choice = slots[1]
            elif label == "commit":
                choice = slots[0]
        if choice or label == "commit":
            conv["stage"] = "confirmed"
            when = f" for {choice}" if choice else ""
            txt = (f"Done ✅ aapka slot{when} confirm ho gaya. {F.biz} mein milte hain!" if hi else
                   f"Done ✅ You're confirmed{when}. See you at {F.biz}!")
            if conv.get("kind") == "chronic_refill_due":
                txt = ("Done ✅ order confirm ho gaya — saved address par delivery ho jayegi." if hi else
                       "Done ✅ Your order is confirmed for delivery to your saved address.")
            return {"action": "send", "body": txt, "cta": "none",
                    "rationale": "Customer accepted; confirmed immediately with the chosen slot."}
        if label == "question":
            txt = (f"Zaroor — {F.biz} ki team aapko thodi der mein call karke details bata degi. Tab tak slot hold kar dein? Reply YES."
                   if hi else f"Of course — someone from {F.biz} will call you shortly with the details. Shall we hold the slot meanwhile? Reply YES.")
            return {"action": "send", "body": txt, "cta": "binary_yes_no",
                    "rationale": "Customer question handed to the merchant's team (no invented answers); booking kept open."}
        return {"action": "send",
                "body": ("Noted 🙏 Koi aur time suit kare to bata dijiye, hum arrange kar denge." if hi else
                         "Noted 🙏 If another time suits you better, just tell us and we'll arrange it."),
                "cta": "open_ended", "rationale": "Customer reply without a clear choice; one open, low-pressure follow-up."}

    def _dedupe(self, conv: dict, body: str) -> str:
        if body not in conv["bot_bodies"]:
            return body
        return body + " (Just resurfacing this in case it got buried.)" if not body.endswith("buried.)") else body + " 🙏"

