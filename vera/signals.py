"""Merchant-state signal extraction and ranking.

A trigger says *why now*; the merchant's state says *what to anchor on*.
When a trigger payload is thin (placeholder triggers in the generated set)
or the strategy needs a supporting fact, we extract every candidate signal
from the merchant context, score it, and take the single best one.

Scoring = magnitude x base weight x mode affinity. Ties break on the signal
name, so results are fully deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import util
from .facts import Facts

METRIC_LABEL = {"views": "profile views", "calls": "calls", "ctr": "click-through rate",
                "directions": "direction requests", "leads": "leads"}

# which signals fit which message mode
MODE_AFFINITY = {
    "problem": {"perf_drop": 1.6, "ctr_below_peer": 1.3, "no_active_offer": 1.2, "stale_posts": 1.1,
                "unverified": 1.1, "neg_review": 1.2, "lapsed_pool": 1.0, "calls_below_peer": 1.1,
                "sub_ending": 0.9, "sub_expired": 1.0, "perf_rise": 0.4, "pos_review": 0.5,
                "views_above_peer": 0.3},
    "positive": {"perf_rise": 1.6, "pos_review": 1.3, "views_above_peer": 1.2, "calls_above_peer": 1.2,
                 "perf_drop": 0.3, "ctr_below_peer": 0.4, "no_active_offer": 0.8, "stale_posts": 0.7,
                 "unverified": 0.6, "neg_review": 0.5, "lapsed_pool": 0.6},
    "neutral": {},
}


@dataclass
class Signal:
    name: str
    score: float
    fact: str                      # grounded sentence fragment
    action: str                    # what Vera proposes to do about it
    numbers: list = field(default_factory=list)
    detail: dict = field(default_factory=dict)


def _metric_signals(F: Facts) -> list[Signal]:
    out = []
    for metric in ("calls", "views", "ctr", "directions", "leads"):
        d = F.delta_of(metric)
        if d is None or abs(d) < 0.08:
            continue
        label = METRIC_LABEL[metric]
        if d < 0:
            out.append(Signal(
                "perf_drop", abs(d) * 10 * (1.2 if metric == "calls" else 1.0),
                f"{label} are down {util.pct(d)} this week" if metric != "ctr"
                else f"your click-through rate slipped {util.pct(d)} this week",
                "refresh your listing so searchers have a reason to call",
                detail={"metric": metric, "delta": d}))
        else:
            out.append(Signal(
                "perf_rise", d * 10 * (1.2 if metric == "calls" else 1.0),
                f"{label} are up {util.pct(d)} this week" if metric != "ctr"
                else f"your click-through rate rose {util.pct(d)} this week",
                "put a fresh post up while interest is warm",
                detail={"metric": metric, "delta": d}))
    return out


def extract(F: Facts) -> list[Signal]:
    sigs: list[Signal] = _metric_signals(F)

    ctr, peer_ctr = F.metric("ctr"), F.peer_avg("ctr")
    if ctr is not None and peer_ctr:
        if ctr < peer_ctr * 0.9:
            gap = (peer_ctr - ctr) / peer_ctr
            sigs.append(Signal("ctr_below_peer", 2 + gap * 8,
                               f"your click-through rate is {util.pct(ctr)} vs a {util.pct(peer_ctr)} peer average",
                               "refresh your photos and lead offer to lift conversions",
                               detail={"ctr": ctr, "peer": peer_ctr}))

    calls, peer_calls = F.metric("calls"), F.peer_avg("calls")
    if calls is not None and peer_calls:
        ratio = calls / peer_calls if peer_calls else 1
        if ratio < 0.6:
            sigs.append(Signal("calls_below_peer", 2 + (1 - ratio) * 5,
                               f"you got {util.num(calls)} calls in the last 30 days vs a peer average of {util.num(peer_calls)}",
                               "add a clear call-to-action offer to your listing",
                               detail={"calls": calls, "peer": peer_calls}))
        elif ratio > 1.4:
            sigs.append(Signal("calls_above_peer", 1.5 + min(ratio, 4),
                               f"you got {util.num(calls)} calls in the last 30 days, well above the {util.num(peer_calls)} peer average",
                               "turn that momentum into reviews",
                               detail={"calls": calls, "peer": peer_calls}))

    views, peer_views = F.metric("views"), F.peer_avg("views")
    if views is not None and peer_views and views > peer_views * 1.3:
        sigs.append(Signal("views_above_peer", 1.5 + min(views / peer_views, 4),
                           f"your listing pulled {util.num(views)} profile views in 30 days vs a {util.num(peer_views)} peer average",
                           "convert that visibility with a sharper offer",
                           detail={"views": views, "peer": peer_views}))

    if not F.active_offer_titles:
        sugg = F.catalog_offer(audience="new_user")
        if sugg:
            sigs.append(Signal("no_active_offer", 4.0,
                               "there's no live offer on your listing right now",
                               f"switch on '{sugg}' from the {F.slug or 'category'} catalog",
                               detail={"suggested": sugg}))

    stale = F.signal_value("stale_posts")
    if stale:
        days = "".join(ch for ch in stale if ch.isdigit())
        if days:
            sigs.append(Signal("stale_posts", 2 + min(int(days), 60) / 10,
                               f"your last Google post went up {days} days ago",
                               "draft fresh Google posts for you to approve",
                               detail={"days": int(days)}))

    if F.identity.get("verified") is False:
        sigs.append(Signal("unverified", 4.5,
                           "your listing is still unverified on Google",
                           "start the Google verification for you"))

    for t in F.review_themes("neg")[:1]:
        n = t.get("occurrences_30d") or 0
        if n >= 2:
            sigs.append(Signal("neg_review", 2 + n,
                               f"{n} reviews this month mention {util.humanize(t.get('theme'))}",
                               "draft calm public replies to those reviews",
                               detail={"theme": t}))
    for t in F.review_themes("pos")[:1]:
        n = t.get("occurrences_30d") or 0
        if n >= 3:
            sigs.append(Signal("pos_review", 1.5 + n / 3,
                               f"{n} reviews this month praise your {util.humanize(t.get('theme'))}",
                               "feature that praise on your listing",
                               detail={"theme": t}))

    for key, window in (("lapsed_180d_plus", "180+ days"), ("lapsed_90d_plus", "90+ days")):
        n = F.agg.get(key)
        if isinstance(n, (int, float)) and n > 0:
            sigs.append(Signal("lapsed_pool", 2 + min(n, 300) / 40,
                               f"{util.num(n)} of your {F.nouns[1]} haven't been back in {window}",
                               f"draft a win-back message for those {F.nouns[1]}",
                               detail={"count": n, "window": window}))
            break

    status = F.sub.get("status")
    dr = F.sub.get("days_remaining")
    if status in ("active", "trial") and isinstance(dr, (int, float)) and 0 < dr <= 14:
        sigs.append(Signal("sub_ending", 3 + (14 - dr) / 3,
                           f"your {F.sub.get('plan') or 'magicpin'} plan ends in {int(dr)} days",
                           "keep your profile upkeep running without a gap"))
    dse = F.sub.get("days_since_expiry")
    if status == "expired" and isinstance(dse, (int, float)) and dse > 0:
        sigs.append(Signal("sub_expired", 3.5,
                           f"your plan lapsed {int(dse)} days ago, so profile upkeep is paused",
                           "restart your profile upkeep"))
    return sigs


def rank(F: Facts, mode: str = "neutral", exclude: tuple = ()) -> list[Signal]:
    aff = MODE_AFFINITY.get(mode, {})
    sigs = [s for s in extract(F) if s.name not in exclude]
    for s in sigs:
        s.score = round(s.score * aff.get(s.name, 1.0), 4)
    return sorted(sigs, key=lambda s: (-s.score, s.name, s.fact))


PROBLEM = {"perf_drop", "ctr_below_peer", "calls_below_peer", "no_active_offer", "stale_posts", "unverified",
           "neg_review", "lapsed_pool", "sub_ending", "sub_expired"}
POSITIVE = {"perf_rise", "pos_review", "views_above_peer", "calls_above_peer"}


def dominant_of(F: Facts, names: set, mode: str = "neutral") -> Optional[Signal]:
    r = [s for s in rank(F, mode) if s.name in names]
    return r[0] if r else None


def dominant(F: Facts, mode: str = "neutral", exclude: tuple = ()) -> Optional[Signal]:
    r = rank(F, mode, exclude)
    return r[0] if r else None
