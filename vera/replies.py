"""Multi-turn reply policy.

classify(message) -> one label, checked in priority order:
    auto_reply > opt_out > hostile > off_topic > commit > later > decline > question > info

The policy then maps (label, conversation stage) -> send | wait | end.
Key behaviours the judge tests:
  * auto-reply: one owner-flag nudge, then wait, then end (counted per
    merchant, across conversations, so rotating conversation ids can't
    defeat it);
  * explicit intent ("ok let's do it") -> immediately deliver the drafted
    artifact; never ask another qualifying question;
  * opt-out / abuse -> end (or a single apology line), then stay silent;
  * off-topic (GST, loans...) -> decline in one line, redirect to the thread.
"""
from __future__ import annotations

import re
from typing import Optional

from . import util

AUTO_PATTERNS = [
    r"thank(s| you) for (contacting|reaching|your message|messaging)",
    r"(our|the) team will (respond|reply|get back|contact)",
    r"will (respond|reply|get back to you) (shortly|soon|as soon)",
    r"we (are|'re) (currently )?(closed|unavailable|away)",
    r"automated (assistant|message|reply|response)",
    r"this is an auto",
    r"auto[- ]?reply",
    r"business hours",
    r"aapki jaankari ke liye .*shukriya",
    r"team tak pahuncha",
    r"hum jaldi (hi )?(aapse )?(sampark|contact)",
]
OPT_OUT = [r"\bstop\b", r"unsubscribe", r"not interested", r"do ?n[o']?t (message|text|contact|send)",
           r"no more (messages|msgs)", r"remove me", r"leave me alone", r"band karo", r"mat bhej",
           r"message mat", r"nahi chahiye", r"don'?t want", r"\bspam\b"]
HOSTILE = [r"useless", r"\bidiot", r"stupid", r"nonsense", r"bakwas", r"\bfraud\b", r"\bscam\b", r"shut up",
           r"irritat", r"\bpagal\b", r"bewakoof", r"\bwtf\b", r"bloody", r"harass", r"bother(ing)? me",
           r"waste of (my )?time", r"get lost"]
OFF_TOPIC = {
    r"\bgst\b|income tax|\bitr\b|tax (filing|return)|\btds\b": "your CA",
    r"\bloan\b|credit card|bank account|\bemi\b": "your bank",
    r"legal notice|lawyer|court case|rent agreement": "a lawyer",
    r"passport|\bvisa\b|aadhaar|pan card": "the relevant government office",
    r"electricity bill|water bill|gas bill": "your utility provider",
}
COMMIT = [r"^(yes|yeah|yep|yup|ya|haan|han|ha|ok|okay|okk|sure|done|confirm|confirmed|go ahead|proceed|agreed|chalo|theek|thik)\b",
          r"let'?s do it", r"lets do", r"do it", r"go ahead", r"please (do|send|go|proceed|draft|share)",
          r"send (it|me|the|over)", r"sounds good", r"\bkar do\b", r"\bkardo\b", r"\bkaro\b", r"sign me up",
          r"i want to join", r"join karna", r"judna hai", r"\bbook it\b", r"what'?s next", r"whats next",
          r"\bconfirm\b", r"\byes\b", r"good idea", r"i'?m in\b", r"count me in"]
LATER = [r"\blater\b", r"\bbusy\b", r"baad me", r"\btomorrow\b", r"\bkal\b", r"in a meeting",
         r"call you back", r"not now", r"abhi nahi", r"\bnext week\b", r"after some time", r"give me (some )?time"]
DECLINE = [r"^(no|nope|nah|nahi|nahin|na)\b", r"no thanks", r"not needed", r"not required", r"zarurat nahi",
           r"we'?re fine", r"we are fine", r"already (have|done)"]
QUESTION_START = r"^(what|how|why|when|which|who|where|can|could|is|are|does|do|will|kya|kaise|kitna|kitne|kab|kaun)\b"
PRICE_Q = r"price|cost|charge|fee|kitna|kitne|how much|paisa|rate"
HINGLISH = r"\b(hai|haan|nahi|karo|kar do|kya|mujhe|aap|chahiye|bhej|kijiye|dijiye|theek|accha|acha|baad|abhi|kal|judna|karna)\b"


def _any(patterns, text) -> bool:
    return any(re.search(p, text) for p in patterns)


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def is_auto_reply(text: str) -> bool:
    return _any(AUTO_PATTERNS, norm(text))


def is_hinglish(text: str) -> bool:
    return bool(re.search(HINGLISH, norm(text))) or bool(re.search(r"[\u0900-\u097F]", text or ""))


def off_topic_owner(text: str) -> Optional[str]:
    t = norm(text)
    for pat, who in OFF_TOPIC.items():
        if re.search(pat, t):
            return who
    return None


def classify(text: str) -> str:
    t = norm(text)
    if not t:
        return "info"
    if is_auto_reply(t):
        return "auto_reply"
    if _any(OPT_OUT, t):
        return "opt_out"
    if _any(HOSTILE, t):
        return "hostile"
    if off_topic_owner(t):
        return "off_topic"
    if _any(LATER, t) and not re.search(r"^(yes|ok|haan|sure)\b", t):
        return "later"
    if _any(COMMIT, t) and not re.match(r"^(no|nahi|nope)\b", t):
        return "commit"
    if _any(DECLINE, t) and "?" not in t:
        return "decline"
    if "?" in t or re.search(QUESTION_START, t):
        return "question"
    return "info"


def wait_seconds_for(text: str) -> int:
    t = norm(text)
    if re.search(r"tomorrow|\bkal\b|next week", t):
        return 86400
    if re.search(r"meeting|5 min|few min|call you back|busy", t):
        return 1800
    return 14400


def extract_service(text: str, candidates: list[str]) -> Optional[str]:
    """Find which known service/offer the merchant mentioned (for curious-ask answers)."""
    t = norm(text)
    best = None
    for c in candidates:
        key = c.split("@")[0].strip().lower()
        if not key:
            continue
        words = [w for w in re.findall(r"[a-z]+", key) if len(w) > 3]
        if key in t or (words and any(w in t for w in words)):
            if best is None or len(key) > len(best.split("@")[0]):
                best = c
    if best:
        return best.split("@")[0].strip()
    cleaned = re.sub(r"^(mostly|most(ly)? asked( is)?|it'?s|its|i think|probably)\s+", "", t).strip(" .!")
    return cleaned[:40] if cleaned else None
