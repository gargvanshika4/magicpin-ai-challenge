"""Output validation — the last line of defence against hallucination.

Every number in a message must be traceable to the input contexts (or be a
number the composer explicitly derived from them). Messages also must have
exactly one CTA, no URLs, no category taboo words and no internal jargon.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import util
from .facts import Facts

SMALL_OK = {1.0, 2.0, 3.0}                 # "Reply 1 / 2", "three posts"
JARGON = re.compile(r"\b(trigger|payload|suppression|signal_|context_id|merchant_id|customer_id)\b|[a-z]+_[a-z]+", re.I)
URL = re.compile(r"https?://|www\.", re.I)
QUALIFYING = ("would you", "do you", "can you tell", "what if", "how about")
MAX_LEN = 1100


@dataclass
class Report:
    ok: bool
    problems: list = field(default_factory=list)
    bad_numbers: list = field(default_factory=list)


def _taboos(F: Facts) -> list[str]:
    out = []
    for t in F.voice.get("vocab_taboo", []) or []:
        t = re.sub(r"\(.*?\)", "", str(t)).strip().lower()
        if t:
            out.append(t)
    return out + ["guaranteed", "100% safe", "miracle"]


def check(body: str, F: Facts, cta: str = "binary_yes_no") -> Report:
    problems = []
    if not body or not body.strip():
        return Report(False, ["empty body"])
    low = body.lower()
    if URL.search(body):
        problems.append("url in body")
    for t in _taboos(F):
        if t in low:
            problems.append(f"taboo phrase: {t}")
    # jargon: ignore quoted review text and hashtags
    unquoted = re.sub(r'"[^"]*"', "", body)
    if JARGON.search(unquoted):
        problems.append(f"internal jargon: {JARGON.search(unquoted).group(0)}")
    allowed = F.allowed_numbers()
    bad = [n for n in util.extract_numbers(body) if n not in SMALL_OK and not util.number_is_grounded(n, allowed)]
    if bad:
        problems.append(f"ungrounded numbers: {bad}")
    qmarks = unquoted.count("?")
    if cta != "open_ended" and qmarks > 1:
        problems.append("more than one question (multiple CTAs)")
    if cta == "open_ended" and qmarks > 2:
        problems.append("too many questions")
    if cta not in ("none",):
        tail = util.sentences(unquoted)[-2:] if unquoted else []
        if not any(("?" in s) or ("reply" in s.lower()) or ("bhej" in s.lower()) for s in tail):
            problems.append("CTA is not in the last sentence")
    if len(body) > MAX_LEN:
        problems.append("too long")
    return Report(not problems, problems, bad)


def repair(body: str, F: Facts, cta: str) -> str:
    """Drop non-final sentences that carry ungrounded numbers; keep the CTA."""
    allowed = F.allowed_numbers()
    sents = util.sentences(body)
    if len(sents) < 2:
        return body
    keep = []
    for i, s in enumerate(sents):
        is_last = i >= len(sents) - 2 and ("?" in s or "reply" in s.lower())
        bad = [n for n in util.extract_numbers(s) if n not in SMALL_OK and not util.number_is_grounded(n, allowed)]
        if bad and not is_last:
            continue
        keep.append(s)
    return " ".join(keep)


def is_qualifying(text: str) -> bool:
    low = (text or "").lower()
    return any(q in low for q in QUALIFYING)
