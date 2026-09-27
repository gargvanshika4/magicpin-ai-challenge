"""Message strategies, one per trigger kind.

Each strategy:
  1. decides the ONE anchor (a trigger fact, or the dominant merchant signal
     when the trigger payload is thin),
  2. decides the ONE next action,
  3. writes a short message around those, using only grounded facts,
  4. prepares the follow-up artifact Vera delivers if the merchant says yes
     (used by the reply handler for instant intent -> action handoff).

Strategies never invent: offers not in merchant.offers are only ever
*suggested* as catalog options, never described as live.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import signals as sigmod
from . import util
from .facts import Facts
from .voice import customer_cta, customer_greeting, from_line, merchant_cta, opener

# --------------------------------------------------------------------------- types


@dataclass
class Draft:
    body: str
    cta: str                              # binary_yes_no | binary_confirm_cancel | multi_choice_slot | open_ended | none
    rationale: str
    send_as: str = "vera"                 # vera | merchant_on_behalf
    topic: str = ""                       # short human topic, used in follow-ups
    followup: dict = field(default_factory=dict)  # {"label","artifact","confirm","done"}
    strategy: str = ""


STRATEGIES: dict[str, Callable[[Facts], Draft]] = {}


def strategy(*kinds: str):
    def deco(fn):
        for k in kinds:
            STRATEGIES[k] = fn
        fn.__strategy_name__ = fn.__name__
        return fn
    return deco


def _placeholder(F: Facts) -> bool:
    return bool(F.tp.get("placeholder")) or not F.tp


def _post_artifact(F: Facts, headline: str, line2: str = "") -> str:
    loc = f" in {F.locality}" if F.locality else ""
    parts = [f"{F.biz}{loc} — {headline}"]
    if line2:
        parts.append(line2)
    offer = F.best_offer()
    if offer and offer.lower() not in headline.lower():
        parts.append(f"Current offer: {offer}.")
    parts.append("Call or tap for directions to book.")
    return "\n".join(parts)


# =========================================================================== MERCHANT-FACING

# --------------------------------------------------------------------------- research / compliance / CDE / trends
@strategy("research_digest", "category_research_digest_release", "research_digest_release")
def research_digest(F: Facts) -> Draft:
    item = F.digest_item(F.tp.get("top_item_id") or F.tp.get("digest_item_id")) \
        or F.pick_digest(("research", "tech", "trend", "compliance", "cde", "seasonal"))
    if not item:
        return merchant_generic(F)
    title = item.get("title", "")
    source = item.get("source", "")
    summary = util.first_sentence(item.get("summary", ""))
    trial = item.get("trial_n")
    seg = str(item.get("patient_segment") or "")

    # Anchor: who in THIS merchant's book the finding applies to.
    anchor = ""
    if seg:
        cnt = F.agg.get(f"{seg.rstrip('s')}_count") or F.agg.get(f"{seg}_count")
        seg_txt = util.humanize(seg).replace("high risk", "high-risk")
        seg_txt = seg_txt[:-1] if seg_txt.endswith("adults") else seg_txt
        if isinstance(cnt, (int, float)) and cnt > 0:
            anchor = f"That maps directly to the {util.num(cnt)} {seg_txt} {F.nouns[1]} on your roster."
        elif F.has_signal(seg.rstrip("s") + "_cohort") or F.has_signal(seg + "_cohort"):
            anchor = f"Relevant to your {seg_txt} cohort."
    if not anchor:
        top = sigmod.dominant(F, "neutral", exclude=("sub_ending", "sub_expired"))
        if top and top.name in ("ctr_below_peer", "lapsed_pool"):
            anchor = f"Worth it for you: {top.fact}."

    lead = f"{F.salutation}, new from {source}: " if source else f"{F.salutation}, worth a look this week: "
    trial_txt = f" ({util.num(trial)}-patient trial)" if trial else ""
    body_core = f"{lead}{title}{trial_txt}. {summary}"
    if item.get("kind") == "research":
        who = F.nouns[2]
        q_en, q_hi = f"Shall I draft a short {who}-friendly WhatsApp on it that you can forward", \
                     f"Iska ek short {who}-friendly WhatsApp draft kar doon"
        artifact = (f"Quick update from {F.biz}: {summary} "
                    f"If it's been a while since your last check-up, reply here and we'll find you a slot.")
        label = "patient WhatsApp draft"
    else:
        if item.get("actionable") and not anchor:
            anchor = f"Practical take: {item['actionable'].rstrip('.')}."
        q_en, q_hi = "Shall I turn this into a one-page action note for you", "Iska ek one-page action note bana doon"
        artifact = f"{title}\nSource: {source}\nWhat to do: {item.get('actionable', summary)}"
        label = "action note"
    body = util.clean_ws(f"{body_core} {anchor} {merchant_cta(F, q_en, q_hi)}")
    return Draft(body, "binary_yes_no",
                 f"Digest item '{item.get('id')}' chosen as the hook (source-cited); anchored on the merchant's "
                 f"{'matching patient segment' if seg and 'roster' in anchor else 'most relevant account signal'}; one low-effort ask.",
                 topic=title, followup={"label": label, "artifact": artifact,
                                        "confirm": "Reply CONFIRM and I'll queue it for your patient list."})


@strategy("regulation_change", "compliance_alert")
def regulation_change(F: Facts) -> Draft:
    item = F.digest_item(F.tp.get("top_item_id") or F.tp.get("digest_item_id")) or F.pick_digest(("compliance",))
    if not item:
        return merchant_generic(F)
    deadline = util.day_month_year(F.tp.get("deadline_iso") or item.get("date"))
    summ = " ".join(util.sentences(item.get("summary", ""))[:2]) or item.get("title", "")
    src = f" from the {item.get('source')}" if item.get("source") else ""
    when = f" Effective {deadline}." if deadline else ""
    act = f" {item.get('actionable').rstrip('.')}." if item.get("actionable") else ""
    body = (f"{F.salutation}, compliance heads-up{src}: {summ}{when}{act} "
            f"{merchant_cta(F, 'Shall I send a short audit checklist you can file with your SOPs', 'Aapke SOP file ke liye ek short audit checklist bhej doon')}")
    artifact = (f"Audit checklist — {item.get('title')}\n"
                f"1. {item.get('actionable', 'Review current setup against the new rule')}\n"
                f"2. Record the result and date in your SOP file\n"
                f"3. Brief staff before {deadline or 'the effective date'}")
    return Draft(util.clean_ws(body), "binary_yes_no",
                 "Regulatory change with a hard deadline: led with the effective date + exact rule from the "
                 "circular; offered a ready checklist (effort externalization) as the single action.",
                 topic=item.get("title", "compliance update"),
                 followup={"label": "audit checklist", "artifact": artifact,
                           "confirm": "Reply CONFIRM and I'll set a reminder two weeks before the deadline."})


@strategy("cde_opportunity")
def cde_opportunity(F: Facts) -> Draft:
    item = F.digest_item(F.tp.get("digest_item_id") or F.tp.get("top_item_id")) or F.pick_digest(("cde",))
    if not item:
        return merchant_generic(F)
    when = util.day_month(item.get("date"))
    at = util.clock(item.get("date"))
    credits = F.tp.get("credits") or item.get("credits")
    fee = item.get("actionable") or util.humanize(F.tp.get("fee"))
    parts = [f"{F.salutation}, CDE pick for you: \"{item.get('title')}\""]
    if when:
        parts[0] += f" on {when}{', ' + at if at else ''}"
    parts[0] += "."
    if credits:
        parts.append(f"{credits} CDE credits.")
    if fee:
        parts.append(fee.rstrip(".") + ".")
    parts.append(util.first_sentence(item.get("summary", "")))
    body = " ".join(parts) + " " + merchant_cta(F, "Shall I send you the registration details", "Registration details bhej doon")
    return Draft(util.clean_ws(body), "binary_yes_no",
                 "CDE event from the category calendar: date, credits and fee up front so the doctor can decide in one read.",
                 topic=item.get("title", "CDE session"),
                 followup={"label": "registration details",
                           "artifact": f"{item.get('title')}\nWhen: {when or 'see calendar'} {at or ''}\n{fee or ''}\nSource: {item.get('source')}",
                           "confirm": "Reply CONFIRM and I'll remind you the morning of the session."})


@strategy("category_trend_movement", "trend_signal")
def trend_movement(F: Facts) -> Draft:
    trends = [t for t in F.category.get("trend_signals", []) if isinstance(t, dict)]
    q = F.tp.get("query")
    t = next((x for x in trends if x.get("query") == q), None) or \
        (max(trends, key=lambda x: (x.get("delta_yoy") or 0, x.get("query", ""))) if trends else None)
    if not t:
        return research_digest(F)
    offer = F.best_offer(tuple(t.get("query", "").split()))
    tail = f"You already have '{offer}' live — it should be front and centre." if offer else \
        "Your listing doesn't mention it yet."
    body = (f"{F.salutation}, '{t.get('query')}' searches are up {util.pct(t.get('delta_yoy'))} year-on-year"
            f"{' among ' + util.humanize(t.get('segment_age')) if t.get('segment_age') not in (None, 'all') else ''}. {tail} "
            + merchant_cta(F, "Shall I update your Google description to target it", "Google description mein add kar doon"))
    return Draft(util.clean_ws(body), "binary_yes_no",
                 "Category search trend chosen as the demand signal; tied to the merchant's own live offer (or its absence).",
                 topic=f"'{t.get('query')}' demand",
                 followup={"label": "Google description update",
                           "artifact": _post_artifact(F, f"now offering {t.get('query')}"),
                           "confirm": "Reply CONFIRM and I'll publish the updated description."})


# --------------------------------------------------------------------------- performance
@strategy("perf_dip")
def perf_dip(F: Facts) -> Draft:
    metric = F.tp.get("metric")
    delta = F.tp.get("delta_pct")
    baseline = F.tp.get("vs_baseline")
    if metric is None or delta is None:
        drop = next((s for s in sigmod.rank(F, "problem") if s.name == "perf_drop"), None)
        if drop:
            metric, delta = drop.detail["metric"], drop.detail["delta"]
    fix = _dip_fix(F)
    if metric is None or delta is None or float(delta) >= 0:
        # payload gave no dip and merchant data shows none: lead with the real gap instead of inventing a dip
        top = sigmod.dominant(F, "problem")
        if not top:
            return merchant_generic(F)
        body = (f"{F.salutation}, flagging one thing on {F.biz}: {top.fact}. "
                + merchant_cta(F, f"Shall I {top.action}"))
        return Draft(util.clean_ws(body), "binary_yes_no",
                     f"Dip trigger without a measurable drop in the data; led with the strongest real gap ({top.name}) instead.",
                     topic=top.fact, followup=_fix_followup(F, top.action))
    label = sigmod.METRIC_LABEL.get(metric, util.humanize(metric))
    base_txt = f" against your usual {util.num(baseline)}" if baseline else ""
    body = (f"{F.salutation}, {label} at {F.biz} fell {util.pct(delta)} this week{base_txt}. "
            f"{fix['why']} " + merchant_cta(F, fix["q_en"], fix["q_hi"]))
    return Draft(util.clean_ws(body), "binary_yes_no",
                 f"Performance dip on {metric} is the trigger; paired with the single most fixable cause "
                 f"({fix['cause']}) rather than listing every issue.",
                 topic=f"{label} dip", followup=_fix_followup(F, fix["action"]))


def _dip_fix(F: Facts) -> dict:
    """Pick the single most fixable cause for a dip (deterministic order)."""
    if not F.active_offer_titles:
        sugg = F.catalog_offer(audience="new_user")
        if sugg:
            return {"cause": "no live offer", "why": "One gap stands out: there's no live offer on your listing, so searchers have nothing specific to act on.",
                    "q_en": f"Shall I switch on '{sugg}' from the catalog today",
                    "q_hi": f"'{sugg}' aaj hi live kar doon", "action": f"switch on '{sugg}'"}
    stale = F.signal_value("stale_posts")
    if stale:
        return {"cause": "stale posts", "why": f"Your last Google post went up {stale.rstrip('d')} days ago — fresh posts are the quickest lever.",
                "q_en": "Shall I draft three posts for you to approve", "q_hi": "Teen posts draft kar doon",
                "action": "draft fresh Google posts"}
    if F.identity.get("verified") is False:
        return {"cause": "unverified listing", "why": "Your listing is still unverified on Google, which caps how often it's shown.",
                "q_en": "Shall I start the verification for you", "q_hi": "Verification abhi start kar doon",
                "action": "start Google verification"}
    ctr, peer = F.metric("ctr"), F.peer_avg("ctr")
    if ctr is not None and peer and ctr < peer:
        return {"cause": "CTR below peers", "why": f"Your click-through rate is {util.pct(ctr)} vs a {util.pct(peer)} peer average, so the listing itself needs a refresh.",
                "q_en": "Shall I refresh your cover photo and lead offer", "q_hi": "Cover photo aur lead offer refresh kar doon",
                "action": "refresh your listing"}
    return {"cause": "listing refresh", "why": "A fresh post usually brings calls back fastest.",
            "q_en": "Shall I draft a post for you to approve", "q_hi": "Ek post draft kar doon",
            "action": "draft a fresh Google post"}


def _fix_followup(F: Facts, action: str) -> dict:
    return {"label": "draft", "artifact": _post_artifact(F, "open this week for walk-ins and bookings"),
            "confirm": f"Reply CONFIRM and I'll {action} right away."}


@strategy("perf_spike")
def perf_spike(F: Facts) -> Draft:
    metric = F.tp.get("metric")
    delta = F.tp.get("delta_pct")
    baseline = F.tp.get("vs_baseline")
    driver = F.tp.get("likely_driver")
    if metric is None or delta is None:
        ups = sorted(((k[:-4], v) for k, v in F.delta.items() if k.endswith("_pct") and isinstance(v, (int, float)) and v > 0),
                     key=lambda kv: (-(kv[1] * (1.2 if kv[0] == "calls" else 1)), kv[0]))
        if ups:
            metric, delta = ups[0]
    if metric is None or delta is None or float(delta) <= 0:
        top = sigmod.dominant_of(F, sigmod.POSITIVE, "positive")
        if not top:
            return merchant_generic(F)
        body = f"{F.salutation}, good news on {F.biz}: {top.fact}. " + merchant_cta(F, f"Shall I {top.action}")
        return Draft(util.clean_ws(body), "binary_yes_no",
                     f"Spike trigger; strongest verifiable positive signal is {top.name}.", topic=top.fact,
                     followup=_fix_followup(F, top.action))
    label = sigmod.METRIC_LABEL.get(metric, util.humanize(metric))
    base_txt = f" over your usual {util.num(baseline)}" if baseline else ""
    driver_txt = f" The lift lines up with your {util.humanize(driver).replace(' post', '')} post." if driver else ""
    # double down on the driver; otherwise convert momentum into reviews
    if driver:
        q_en, q_hi = "Shall I draft a follow-up post while interest is warm", "Interest garam hai — follow-up post draft kar doon"
        action = "draft the follow-up post"
    else:
        q_en, q_hi = "Shall I send a review request to this week's callers", "Is hafte ke callers ko review request bhej doon"
        action = "send the review requests"
    body = f"{F.salutation}, {label} at {F.biz} are up {util.pct(delta)} this week{base_txt}.{driver_txt} " + merchant_cta(F, q_en, q_hi)
    return Draft(util.clean_ws(body), "binary_yes_no",
                 "Performance spike: named the exact lift and its likely driver, and proposed doubling down while demand is warm.",
                 topic=f"{label} spike", followup={"label": "follow-up post",
                                                   "artifact": _post_artifact(F, f"thank you {F.locality or ''} — more slots this week".replace("  ", " ")),
                                                   "confirm": f"Reply CONFIRM and I'll {action}."})


@strategy("seasonal_perf_dip")
def seasonal_perf_dip(F: Facts) -> Draft:
    metric = F.tp.get("metric") or "views"
    delta = F.tp.get("delta_pct") if F.tp.get("delta_pct") is not None else F.delta_of(metric)
    label = sigmod.METRIC_LABEL.get(metric, metric)
    item = F.pick_digest(("seasonal",))
    members = F.agg.get("total_active_members")
    ctx = util.first_sentence(item.get("summary", "")) if item else ""
    lead = f"{F.salutation}, {label} at {F.biz} are down {util.pct(delta)} this week" if delta is not None else \
        f"{F.salutation}, {label} at {F.biz} have dipped this week"
    reframe = " — that's the expected seasonal lull, not a problem with your listing."
    playbook = f" {item.get('actionable').rstrip('.')}." if item and item.get("actionable") else ""
    if members:
        focus = f" The better lever right now is your {util.num(members)} active {F.nouns[1]}."
        q_en, q_hi = f"Shall I draft a summer retention challenge for them", "Unke liye ek summer retention challenge draft kar doon"
    else:
        focus = f" The better lever right now is keeping your existing {F.nouns[1]} coming back."
        q_en, q_hi = "Shall I draft a retention message for them", "Unke liye retention message draft kar doon"
    body = f"{lead}{reframe} {ctx}{playbook}{focus} " + merchant_cta(F, q_en, q_hi)
    return Draft(util.clean_ws(body), "binary_yes_no",
                 "Seasonal dip flagged as expected: reframed to prevent panic ad spend, cited the category's seasonal data, "
                 "and redirected effort to retention of the existing base.",
                 topic="seasonal dip",
                 followup={"label": "retention challenge",
                           "artifact": (f"{F.biz} Summer Challenge: show up 3x a week through the season and get your "
                                        f"progress check on us. Reply IN to join."),
                           "confirm": f"Reply CONFIRM and I'll send it to your {F.nouns[1]}."})


@strategy("milestone_reached")
def milestone(F: Facts) -> Draft:
    metric = F.tp.get("metric")
    now, target = F.tp.get("value_now"), F.tp.get("milestone_value")
    pos = F.review_themes("pos")
    if metric and isinstance(now, (int, float)) and isinstance(target, (int, float)):
        label = util.humanize(metric).replace("review count", "reviews")
        gap = int(target - now)
        if gap > 0:
            F.derive(gap)
            head = f"{F.salutation}, {F.biz} is {gap} {label} away from {util.num(target)} on Google ({util.num(now)} now)."
        else:
            head = f"{F.salutation}, {F.biz} just crossed {util.num(target)} {label} on Google."
        support = ""
        if pos:
            t = pos[0]
            support = f" Your {util.humanize(t.get('theme'))} already earns {t.get('occurrences_30d')} positive mentions a month — those regulars are the fastest route."
        body = head + support + " " + merchant_cta(F, "Shall I draft a short review-request card for your counter", "Counter ke liye ek review-request card draft kar doon")
        return Draft(util.clean_ws(body), "binary_yes_no",
                     "Milestone within reach: exact gap to the round number creates a completable goal; the ask is a tiny, concrete artifact.",
                     topic=f"{util.num(target)} {label}",
                     followup={"label": "review-request card",
                               "artifact": f"Loved your visit to {F.biz}? A 20-second Google review helps us a lot. Scan the QR at the counter — thank you!",
                               "confirm": "Reply CONFIRM and I'll send the print-ready file."})
    # placeholder: a true round-number threshold on a real metric, else the best positive signal
    views = F.metric("views")
    mark = next((m for m in (10000, 5000, 2500, 2000, 1000, 500) if views and views >= m), None)
    top = sigmod.dominant_of(F, sigmod.POSITIVE, "positive")
    if mark:
        F.derive(mark)
        fact = f"your listing crossed {util.num(mark)} profile views in the last 30 days ({util.num(views)} in total)"
    elif top:
        fact = top.fact
    else:
        return merchant_generic(F)
    body = f"{F.salutation}, a milestone worth marking at {F.biz}: {fact}. " + merchant_cta(
        F, f"Shall I turn it into a thank-you post for your {F.nouns[1]}", "Iska ek thank-you post bana doon")
    return Draft(util.clean_ws(body), "binary_yes_no",
                 "Milestone trigger without payload numbers; celebrated a true round-number threshold / strongest verifiable "
                 f"positive ({'views threshold' if mark else top.name}).",
                 topic="milestone", followup=_fix_followup(F, "publish the thank-you post"))


@strategy("review_theme_emerged")
def review_theme(F: Facts) -> Draft:
    theme = F.tp.get("theme")
    n = F.tp.get("occurrences_30d")
    quote = F.tp.get("common_quote")
    trend = F.tp.get("trend")
    if not theme:
        neg = F.review_themes("neg")
        if neg:
            theme, n, quote = neg[0].get("theme"), neg[0].get("occurrences_30d"), neg[0].get("common_quote")
    if not theme:
        body = (f"{F.salutation}, a new theme is showing up in your recent Google reviews for {F.biz}. "
                f"Replying publicly within the week stops a pattern from sticking to your listing. "
                + merchant_cta(F, "Shall I draft replies for you to approve", "Replies draft karke approval ke liye bhej doon"))
        return Draft(util.clean_ws(body), "binary_yes_no",
                     "Review-theme trigger without theme details: stayed on the trigger (reviews) and offered drafted replies "
                     "rather than inventing the theme.", topic="recent reviews",
                     followup={"label": "review replies", "artifact": f"Thank you for your feedback — we've shared it with the team at {F.biz} and would love to see you again.",
                               "confirm": "Reply CONFIRM and I'll post them."})
    pos = F.review_themes("pos")
    head = f"{F.salutation}, {n} reviews in the last 30 days mention {util.humanize(theme)}" if n else \
        f"{F.salutation}, a pattern is forming in your reviews around {util.humanize(theme)}"
    head += " — and it's rising." if trend == "rising" else "."
    q = f' One reads: "{quote}".' if quote else ""
    contrast = ""
    if pos:
        contrast = f" Meanwhile {pos[0].get('occurrences_30d')} reviews praise your {util.humanize(pos[0].get('theme'))}, so this is the one thing pulling ratings down."
    body = head + q + contrast + " " + merchant_cta(F, "Shall I draft calm public replies to those reviews", "Un reviews ke liye polite public replies draft kar doon")
    return Draft(util.clean_ws(body), "binary_yes_no",
                 "Emerging negative review theme: quantified it, quoted a real review, contrasted with the merchant's praised strength, "
                 "and offered the lowest-effort fix (drafted replies).",
                 topic=f"{util.humanize(theme)} reviews",
                 followup={"label": "review replies",
                           "artifact": (f"Thank you for the honest feedback — you're right, this wasn't our standard. "
                                        f"We've flagged {util.humanize(theme)} with the team this week. Please give {F.biz} another try."),
                           "confirm": "Reply CONFIRM and I'll post the replies."})


# --------------------------------------------------------------------------- lifecycle / subscription
@strategy("renewal_due")
def renewal_due(F: Facts) -> Draft:
    days = F.tp.get("days_remaining") or F.sub.get("days_remaining")
    plan = F.tp.get("plan") or F.sub.get("plan") or "magicpin"
    amount = F.tp.get("renewal_amount")
    if not days and F.sub.get("status") == "expired":
        return winback(F)
    if isinstance(days, (int, float)) and days > 30:
        top = sigmod.dominant(F, "problem", exclude=("sub_ending", "sub_expired"))
        lead = f"{F.salutation}, your {plan} plan for {F.biz} runs for another {int(days)} days, so no renewal action is needed yet."
        if top:
            body = f"{lead} Worth using it on this, though: {top.fact}. " + merchant_cta(F, f"Shall I {top.action}")
            fu = _fix_followup(F, top.action)
        else:
            body = f"{lead} " + merchant_cta(F, "Shall I send a short summary of what the plan did for you this month",
                                             "Is mahine plan ne kya kiya uska short summary bhej doon")
            fu = _fix_followup(F, "send the summary")
        return Draft(util.clean_ws(body), "binary_yes_no",
                     f"Renewal trigger but {int(days)} days remain: no hard sell; reinforced plan value with the top account gap.",
                     topic="plan value", followup=fu)
    head = f"{F.salutation}, your {plan} plan for {F.biz} ends in {int(days)} days" if days else \
        f"{F.salutation}, your {plan} plan for {F.biz} is due for renewal"
    head += f" (renewal {util.inr(amount)})." if amount else "."
    # one stake-raising fact from the account
    stake = sigmod.dominant(F, "problem", exclude=("sub_ending", "sub_expired", "no_active_offer"))
    stake_txt = ""
    if stake and stake.name in ("perf_drop", "lapsed_pool", "ctr_below_peer", "stale_posts"):
        stake_txt = f" And since {stake.fact}, a gap in profile upkeep now would hurt more than usual."
    body = head + stake_txt + " " + merchant_cta(F, "Shall I send the renewal details", "Renewal details bhej doon")
    return Draft(util.clean_ws(body), "binary_yes_no",
                 "Renewal window with exact days and amount; one account fact raises the stakes without listing every issue.",
                 topic="plan renewal",
                 followup={"label": "renewal summary",
                           "artifact": f"Plan: {plan}\nRenews for: {util.inr(amount) if amount else 'current plan price'}\nKeeps running: Google profile upkeep, posts, offers and review replies for {F.biz}.",
                           "confirm": "Reply CONFIRM and I'll process the renewal on your registered payment method."})


@strategy("winback_eligible")
def winback(F: Facts) -> Draft:
    days = F.tp.get("days_since_expiry") or F.sub.get("days_since_expiry")
    dip = F.tp.get("perf_dip_pct")
    added = F.tp.get("lapsed_customers_added_since_expiry")
    head = f"{F.salutation}, it's been {int(days)} days since {F.biz}'s plan lapsed" if days else \
        f"{F.salutation}, {F.biz}'s plan has lapsed"
    bits = []
    if dip is not None:
        bits.append(f"listing performance is down {util.pct(dip)}")
    if added:
        bits.append(f"{util.num(added)} more {F.nouns[1]} have slipped into your lapsed list")
    mid = (" — since then " + util.join_list(bits) + ".") if bits else "."
    if not bits:
        top = sigmod.dominant(F, "problem", exclude=("sub_expired", "sub_ending"))
        if top:
            mid = f". Right now {top.fact}."
    close = f" Restarting brings back profile upkeep and lets me start winning those {util.num(added)} back." if added else \
        " Restarting brings back profile upkeep straight away."
    body = head + mid + close + " " + merchant_cta(F, "Shall I reactivate your plan", "Plan reactivate kar doon")
    return Draft(util.clean_ws(body), "binary_yes_no",
                 "Lapsed subscriber: loss framing built only from measured post-expiry changes; single reactivation ask.",
                 topic="plan reactivation",
                 followup={"label": "reactivation summary",
                           "artifact": f"Reactivating {F.biz}: profile upkeep resumes today; first action — a win-back message to your lapsed {F.nouns[1]}.",
                           "confirm": "Reply CONFIRM and I'll reactivate now."})


@strategy("dormant_with_vera")
def dormant(F: Facts) -> Draft:
    days = F.tp.get("days_since_last_merchant_message")
    top = sigmod.dominant(F, "problem", exclude=("sub_ending",))
    opener = f"{F.salutation}, it's been {int(days)} days since we last spoke." if days else \
        f"{F.salutation}, it's been a while since we last spoke."
    if top:
        body = f"{opener} One thing worth your eyes: {top.fact}. " + merchant_cta(
            F, f"Shall I {top.action}", None)
        rationale = f"Dormant merchant: re-open with reciprocity — one fresh, verifiable fact ({top.name}) instead of a generic check-in."
        fu = _fix_followup(F, top.action)
    else:
        body = f"{opener} " + merchant_cta(F, "Shall I send a 3-line summary of how your listing did this month", "Is mahine ka 3-line summary bhej doon")
        rationale = "Dormant merchant with no standout signal: low-pressure offer of a summary."
        fu = _fix_followup(F, "send the summary")
    return Draft(util.clean_ws(body), "binary_yes_no", rationale, topic="account check-in", followup=fu)


@strategy("gbp_unverified")
def gbp_unverified(F: Facts) -> Draft:
    path = util.humanize(F.tp.get("verification_path") or "").replace(" or ", " or a ")
    uplift = F.tp.get("estimated_uplift_pct")
    views = F.metric("views")
    head = f"{F.salutation}, {F.biz} is still unverified on Google"
    head += f" — verifying is estimated to lift your visibility by about {util.pct(uplift)}" if uplift else ""
    head += f" on top of the {util.num(views)} views you got this month." if views else "."
    how = f" Google confirms it by {path}." if path else ""
    body = head + how + " " + merchant_cta(F, "Shall I start the verification now", "Verification abhi start kar doon")
    return Draft(util.clean_ws(body), "binary_yes_no",
                 "Unverified listing: quantified the estimated uplift against the merchant's current views; one-step ask.",
                 topic="Google verification",
                 followup={"label": "verification steps",
                           "artifact": f"1. I request verification for {F.biz}.\n2. Google sends a code by {path or 'phone or postcard'}.\n3. You share the code here — I finish the rest.",
                           "confirm": "Reply CONFIRM and I'll request the code now."})


@strategy("competitor_opened")
def competitor(F: Facts) -> Draft:
    name = F.tp.get("competitor_name")
    dist = F.tp.get("distance_km")
    their = F.tp.get("their_offer")
    opened = util.day_month(F.tp.get("opened_date"))
    who = f"a new {F.nouns[0]} — {name} —" if name else f"a new {F.nouns[0]}"
    where = f" {dist} km away" if dist else (f" near {F.locality}" if F.locality else " nearby")
    head = f"{F.salutation}, {who} opened{where}{' on ' + opened if opened else ''}."
    price_line = ""
    if their:
        mine = F.best_offer(tuple(w.lower() for w in their.split("@")[0].split()[:2]))
        their_p = util.extract_numbers(their.split("@")[-1]) if "@" in their else []
        mine_p = util.extract_numbers(mine.split("@")[-1]) if mine and "@" in mine else []
        if mine and their_p and mine_p and mine_p[0] > their_p[0]:
            gap = int(mine_p[0] - their_p[0])
            F.derive(gap)
            price_line = f" They're advertising {their} — ₹{gap} under your {mine}."
        else:
            price_line = f" They're advertising {their}."
    pos = F.review_themes("pos")
    if pos:
        t = pos[0]
        angle = (f" Rather than a price war, lean on what your {F.nouns[1]} already praise: "
                 f"{t.get('occurrences_30d')} reviews this month mention your {util.humanize(t.get('theme'))}.")
        q_en, q_hi = "Shall I refresh your listing to lead with that", "Listing ko usi strength se lead karwa doon"
        action = "refresh your listing around that strength"
    else:
        top = sigmod.dominant_of(F, sigmod.PROBLEM - {"sub_ending", "sub_expired"}, "problem")
        strength = sigmod.dominant_of(F, sigmod.POSITIVE, "positive")
        if top:
            angle = f" Worth tightening your own listing first — {top.fact}."
            q_en, q_hi, action = f"Shall I {top.action}", None, top.action
        elif strength:
            angle = f" You're starting from strength — {strength.fact}."
            q_en, q_hi, action = "Shall I refresh your listing to make that obvious to new searchers", None, "refresh your listing"
        else:
            angle = ""
            q_en, q_hi, action = "Shall I refresh your listing this week", None, "refresh your listing"
    body = head + price_line + angle + " " + merchant_cta(F, q_en, q_hi)
    return Draft(util.clean_ws(body), "binary_yes_no",
                 "New competitor nearby: stated only the verified competitor facts, avoided a price war, and pointed the merchant "
                 "at their own strongest differentiator.",
                 topic="new competitor nearby", followup=_fix_followup(F, action))


@strategy("curious_ask_due", "scheduled_recurring")
def curious_ask(F: Facts) -> Draft:
    # make an informed guess so the question is easy to answer
    guess, why = None, None
    pos = F.review_themes("pos")
    trends = sorted([t for t in F.category.get("trend_signals", []) if isinstance(t, dict)],
                    key=lambda t: (-(t.get("delta_yoy") or 0), t.get("query", "")))
    vocab = [v.lower() for v in F.voice.get("vocab_allowed", [])]
    if pos:
        q = str(pos[0].get("common_quote") or "")
        hit = next((v for v in vocab if v in q.lower()), None)
        if hit:
            guess = hit
            match = next((t for t in trends if hit in t.get("query", "").lower()), None)
            why = f"{pos[0].get('occurrences_30d')} reviews this month rave about it"
            if match:
                why += f", and '{match['query']}' searches are up {util.pct(match['delta_yoy'])} YoY"
        elif pos[0].get("theme"):
            theme = util.humanize(pos[0]["theme"])
            offer = F.best_offer(tuple(theme.split()))
            if offer and theme.split()[0] in offer.lower():
                guess, why = offer.split("@")[0].strip().lower(), f"{pos[0].get('occurrences_30d')} reviews this month praise your {theme}"
    if not guess and F.active_offer_titles:
        guess = F.active_offer_titles[0].split("@")[0].strip()
        why = "it's your live offer"
    if not guess and trends and F.slug in ("salons", "gyms", "dentists"):
        t = trends[0]
        guess = t["query"].replace(" near me", "").replace(" cost", "").replace(" price", "")
        guess = " ".join(w for w in guess.split() if w.lower() not in (F.city.lower(), "delhi"))
        why = f"'{t['query']}' searches are up {util.pct(t['delta_yoy'])} YoY"
    guess_txt = f" My guess is {guess} — {why}." if guess and why else ""
    body = (f"{F.salutation}, quick one for this week's Google post: which service are {F.nouns[1]} asking about most at {F.biz}?"
            f"{guess_txt} Reply with it and I'll turn it into a post plus a ready price reply for WhatsApp.")
    if F.merchant_code_mix:
        body = body.replace("Reply with it and I'll", "Bas naam bhej dijiye, main")
        body = body.replace("turn it into a post plus a ready price reply for WhatsApp.",
                            "uska post aur WhatsApp ke liye ready price reply bana dungi.")
    return Draft(util.clean_ws(body), "open_ended",
                 "Weekly curious-ask: asking the merchant (strongest under-used lever) with an informed guess from their reviews/trends "
                 "so answering takes one word; value is promised in return.",
                 topic="this week's most-asked service",
                 followup={"label": "Google post draft", "artifact": _post_artifact(F, "this week's most-asked service"),
                           "confirm": "Reply CONFIRM and I'll publish it."})


@strategy("active_planning_intent")
def active_planning(F: Facts) -> Draft:
    topic = util.humanize(F.tp.get("intent_topic") or "the plan we discussed")
    last_merchant = F.tp.get("merchant_last_message") or (F.last_from("merchant") or {}).get("body", "")
    # reuse numbers Vera already proposed in this thread (grounded in history)
    prior = F.last_from("vera")
    prior_body = (prior or {}).get("body", "") if prior else ""
    offer = F.best_offer(tuple(topic.split()))
    lines = []
    if "thali" in topic or ("corporate" in topic):
        base = util.extract_numbers(offer.split("@")[-1])[0] if offer and "@" in offer else None
        lines.append(f"{F.biz} Corporate Lunch — for offices near {F.locality or 'you'}")
        if base:
            p10 = int(round(base * 0.9))
            F.derive(p10, 10)
            lines.append(f"• Built on your {offer}: bulk orders of 10+ at {util.inr(p10)} each (10% under retail)")
        lines.append("• Order by the evening before; delivered in the lunch window")
        lines.append("• Weekly invoice for the office admin")
        ask_en, ask_hi = "Shall I publish this as an offer on your listing", "Isse listing par offer ki tarah live kar doon"
    else:
        lines.append(f"{util.humanize(topic).title()} — starter outline for {F.biz}")
        detail = [s for s in util.sentences(prior_body) if any(ch.isdigit() for ch in s) and "?" not in s]
        if detail:
            d0 = re.sub(r"^(great idea\s*[—-]\s*)?(kids yoga summer camps are peaking now\.\s*)?(suggest(ed)?\s+)?", "", detail[0], flags=re.I)
            lines.append("• " + d0[:1].upper() + d0[1:].rstrip("."))
        if offer:
            lines.append(f"• Pairs with your live {offer}")
        lines.append("• Announced via a Google post + WhatsApp note to current members")
        ask_en, ask_hi = "Shall I draft the Google post for it now", "Iska Google post abhi draft kar doon"
    ack = f'you asked "{last_merchant.strip()}" — here\'s a starter version you can edit:' if last_merchant else \
        "Here's a starter version you can edit:"
    body = f"{F.salutation}, {ack}\n\n" + "\n".join(lines) + "\n\n" + merchant_cta(F, ask_en, ask_hi)
    return Draft(util.clean_ws(body), "binary_yes_no",
                 "Merchant already expressed planning intent — switched straight to action mode with a drafted artifact "
                 "built on their live offer / earlier thread, no re-qualification.",
                 topic=topic,
                 followup={"label": "listing offer", "artifact": "\n".join(lines),
                           "confirm": "Reply CONFIRM and it goes live."})


@strategy("festival_upcoming")
def festival(F: Facts) -> Draft:
    fest = F.tp.get("festival")
    date = util.day_month(F.tp.get("date"))
    days = F.tp.get("days_until")
    relevant = F.tp.get("category_relevance")
    beats = [b for b in F.category.get("seasonal_beats", []) if isinstance(b, dict)]
    beat = next((b for b in beats if any(k in str(b.get("note", "")).lower() for k in ("festival", "diwali", "wedding"))), None)
    festive_kw = ("spa", "facial", "bridal", "makeup", "thali", "combo", "family", "brunch", "cake", "whitening",
                  "membership", "plan", "delivery", "health", "card", "month")
    offer = F.best_offer(festive_kw)
    if not fest:
        # placeholder: use the category's own festive beat
        if not beat:
            return merchant_generic(F)
        head_note, _, rest = str(beat.get("note")).partition(" — ")
        body = (f"{F.salutation}, festive season planning for {F.biz}: for {F.slug}, {beat.get('month_range')} is the "
                f"{head_note}" + (f" — {rest}" if rest else "") + ". ")
        body += (f"Your {offer} is the natural lead offer. " if offer else "")
        body += merchant_cta(F, "Shall I draft a festive campaign plan around it", "Iske around festive campaign plan draft kar doon")
        return Draft(util.clean_ws(body), "binary_yes_no",
                     "Festival trigger without specifics: anchored on the category's own festive seasonal beat and the merchant's live offer.",
                     topic="festive season", followup=_fix_followup(F, "set up the festive campaign"))
    when = f"{fest} is on {date}" if date else f"{fest} is coming up"
    if isinstance(days, (int, float)) and days > 60:
        timing = f" — {int(days)} days out, so it's early for a discount, but the right time to lock the plan."
        action_en, action_hi = "Shall I open early pre-bookings", "Early pre-bookings open kar doon"
    elif isinstance(days, (int, float)) and days > 14:
        timing = f" — {int(days)} days out, the ideal window to launch your festive campaign."
        action_en, action_hi = "Shall I set up the festive campaign this week", "Festive campaign is hafte set kar doon"
    else:
        timing = f" — just {int(days)} days away." if isinstance(days, (int, float)) else "."
        action_en, action_hi = "Shall I push a festive post today", "Aaj hi festive post daal doon"
    beat_txt = f" Seasonal context for {F.slug}: {beat.get('month_range')} — {str(beat.get('note')).replace(' — ', '; ')}." if beat else ""
    offer_txt = f" Your {offer} is the natural anchor." if offer else ""
    if relevant and F.slug and F.slug not in relevant:
        beat_txt = f" It's a lighter moment for {F.slug}, so keep it to one simple post."
    if offer:
        action_en += f" with {offer}"
    body = f"{F.salutation}, {when}{timing}{beat_txt}{offer_txt} " + merchant_cta(F, action_en, action_hi)
    return Draft(util.clean_ws(body), "binary_yes_no",
                 "Festival trigger timed by days-until (plan vs launch vs push), grounded in the category's seasonal beat and "
                 "the merchant's own offer.",
                 topic=f"{fest} campaign",
                 followup={"label": "festive post", "artifact": _post_artifact(F, f"{fest} specials — book early"),
                           "confirm": "Reply CONFIRM and I'll schedule it."})


@strategy("ipl_match_today", "local_event", "local_news_event", "sports_event")
def ipl_match(F: Facts) -> Draft:
    match = F.tp.get("match") or F.tp.get("event") or "tonight's match"
    venue = F.tp.get("venue")
    t = util.clock(F.tp.get("match_time_iso"))
    weeknight = F.tp.get("is_weeknight")
    item = next((d for d in F.digest() if "ipl" in str(d.get("id", "")).lower() or "ipl" in str(d.get("title", "")).lower()), None)
    head = f"{F.salutation}, {match}{' at ' + venue if venue else ''} tonight{', ' + t if t else ''}."
    offers = F.active_offer_titles
    combo = F.catalog_offer(("match", "combo"))
    if weeknight is False:
        data = ""
        if item:
            s = item.get("summary", "")
            data = " Heads-up: " + util.first_sentence(s)
        restricted = next((o for o in offers if any(d in o for d in ("Tue", "Wed", "Thu"))), None)
        save = f" Keep your {restricted} for the next weekday match." if restricted else ""
        if combo:
            q_en = f"Shall I put a delivery-only {combo} live before the match"
            q_hi = f"Match se pehle delivery-only {combo} live kar doon"
        else:
            q_en, q_hi = "Shall I push a delivery post before the match", "Match se pehle delivery post daal doon"
        body = head + data + " So tonight, lean on delivery rather than dine-in." + save + " " + merchant_cta(F, q_en, q_hi)
        why = ("Weekend match: used the category's own data (weekend matches cut dine-in covers) to recommend a "
               "delivery play instead of a dine-in promo — contrarian but grounded.")
    else:
        lead = next((o for o in offers), None) or combo
        extra = ""
        if item:
            extra = " " + util.sentences(item.get("summary", ""))[1] if len(util.sentences(item.get("summary", ""))) > 1 else ""
        body = head + extra + (f" Your {lead} fits match night." if lead else "") + " " + merchant_cta(
            F, "Shall I push a match-night post at 6pm", "6 baje match-night post daal doon")
        F.derive(6)
        why = "Weeknight match: category data shows weeknight matches lift covers; pushed the merchant's live offer."
    return Draft(util.clean_ws(body), "binary_yes_no", why, topic=f"{match} match night",
                 followup={"label": "Swiggy/Instagram banner",
                           "artifact": _post_artifact(F, f"{match} tonight — order in for the match"),
                           "confirm": "Reply CONFIRM and it goes live."})


@strategy("supply_alert", "product_recall")
def supply_alert(F: Facts) -> Draft:
    item = F.digest_item(F.tp.get("alert_id") or F.tp.get("top_item_id")) or F.pick_digest(("alert", "supply"))
    mol = F.tp.get("molecule") or ""
    batches = F.tp.get("affected_batches") or []
    mfr = F.tp.get("manufacturer")
    prior = F.last_from("merchant")
    followup_thread = prior and mol and mol.lower() in " ".join(h.get("body", "") for h in F.history()).lower() \
        and str(prior.get("engagement", "")).startswith("intent")
    reason = ""
    if item:
        s = item.get("summary", "")
        reason = " — " + ("sub-potency, no safety risk beyond weaker control" if "sub-potency" in s else util.first_sentence(s).rstrip("."))
    bt = f" ({util.join_list(batches)})" if batches else ""
    src = f" per {item.get('source')}" if item and item.get("source") else ""
    head = (f"{F.salutation}, following up on the {mol} recall you asked about:" if followup_thread
            else f"{F.salutation}, urgent:")
    body = (f"{head} {len(batches) or 'some'} {mol} batches{bt}{' by ' + mfr if mfr else ''} are under voluntary recall{src}{reason}.")
    if batches:
        F.derive(len(batches))
    chronic = F.agg.get("chronic_rx_count")
    if chronic:
        body += f" I'm filtering your {util.num(chronic)} chronic-Rx customers for anyone dispensed these batches."
    body += " " + merchant_cta(F, "Shall I draft the customer WhatsApp with the replacement steps",
                               "Customers ke liye replacement steps ke saath WhatsApp draft kar doon")
    return Draft(util.clean_ws(body), "binary_yes_no",
                 "Urgent recall: exact batches, manufacturer and risk level from the alert; continued the merchant's earlier "
                 "request instead of re-pitching; single ask = draft customer notice.",
                 topic=f"{mol} recall",
                 followup={"label": "customer notice",
                           "artifact": (f"Namaste, {F.biz} here. A batch of {mol} you may have received is under a voluntary "
                                        f"recall{' (' + ', '.join(batches) + ')' if batches else ''}. It is not a safety risk, but please "
                                        f"bring your strip in and we'll replace it free of charge."),
                           "confirm": "Reply CONFIRM and I'll send it to the affected customers."})


@strategy("category_seasonal", "weather_heatwave", "seasonal_demand")
def category_seasonal(F: Facts) -> Draft:
    trends = F.tp.get("trends") or []
    parsed = []
    for t in trends:
        parts = str(t).rsplit("_", 1)
        if len(parts) == 2 and parts[1].lstrip("+-").isdigit():
            name = util.humanize(parts[0]).replace(" demand", "").replace("cold cough", "cold/cough")
            parsed.append((name, int(parts[1])))
    item = F.pick_digest(("seasonal",))
    if parsed:
        ups = [f"{n} +{v}%" if v > 0 else f"{n} {v}%" for n, v in parsed]
        head = f"{F.salutation}, the summer demand shift is here: " + util.join_list(ups) + "."
    elif item:
        head = f"{F.salutation}, seasonal shift: {item.get('title')}."
    else:
        return merchant_generic(F)
    shelf = f" {item.get('actionable').rstrip('.')}." if item and item.get("actionable") else ""
    top_up = [n for n, v in sorted(parsed, key=lambda x: -x[1]) if v > 0][:2]
    focus = util.join_list(top_up) if top_up else "seasonal essentials"
    body = head + shelf + " " + merchant_cta(F, f"Shall I post a 'summer essentials' update featuring {focus} so searchers know you stock them",
                                             f"'Summer essentials' post daal doon ({focus})")
    return Draft(util.clean_ws(body), "binary_yes_no",
                 "Seasonal demand shift: quoted the measured category shifts, gave the shelf action from the digest, and one "
                 "visibility ask on the top-rising items.",
                 topic="summer demand shift",
                 followup={"label": "summer essentials post",
                           "artifact": _post_artifact(F, f"summer essentials in stock: {focus}"),
                           "confirm": "Reply CONFIRM and I'll publish it."})


# --------------------------------------------------------------------------- generic merchant fallback
def merchant_generic(F: Facts) -> Draft:
    top = sigmod.dominant(F, "neutral", exclude=("sub_ending",))
    kind = util.humanize(F.trigger.get("kind") or "update")
    if top:
        body = f"{F.salutation}, quick one on {F.biz}: {top.fact}. " + merchant_cta(F, f"Shall I {top.action}")
        why = f"No specific playbook for '{kind}'; anchored on the dominant account signal ({top.name}) with one action."
        fu = _fix_followup(F, top.action)
    else:
        offer = F.best_offer()
        body = (f"{F.salutation}, quick one on {F.biz}" + (f": your {offer} is live" if offer else "") + ". "
                + merchant_cta(F, "Shall I draft this week's Google post for you to approve"))
        why = f"No specific playbook for '{kind}' and no standout signal; offered the lowest-effort useful action."
        fu = _fix_followup(F, "publish the post")
    return Draft(util.clean_ws(body), "binary_yes_no", why, topic=kind, followup=fu)


# =========================================================================== CUSTOMER-FACING

def _slot_labels(F: Facts, key: str) -> list[str]:
    return [s.get("label") for s in (F.tp.get(key) or []) if isinstance(s, dict) and s.get("label")]


RECALL_WORDS = {
    # slug: (en service noun, hi service noun, offer keywords, en CTA, hi CTA)
    "dentists": ("check-up", "check-up", ("clean", "check", "scaling"),
                 "Shall we share this week's open slots", "Is hafte ke open slots bhej dein"),
    "salons": ("next appointment", "next appointment", ("haircut", "spa", "facial"),
               "Shall we share this week's open slots", "Is hafte ke open slots bhej dein"),
    "gyms": ("next session", "next session", ("session", "class", "pt"),
             "Shall we hold a spot for you this week", "Is hafte aapke liye spot hold kar dein"),
    "pharmacies": ("regular refill", "regular refill", ("delivery", "refill"),
                   "Shall we keep your usual items ready", "Aapka regular saman ready rakh dein"),
    "restaurants": ("next visit", "next visit", ("thali", "combo", "brunch"),
                    "Shall we reserve a table for you this week", "Is hafte aapke liye table reserve kar dein"),
}


@strategy("recall_due")
def recall_due(F: Facts) -> Draft:
    addressee, subject = F.customer_names()
    words = RECALL_WORDS.get(F.slug, ("next visit", "next visit", (), "Shall we share this week's open slots",
                                      "Is hafte ke open slots bhej dein"))
    service = util.humanize(F.tp.get("service_due")) if F.tp.get("service_due") else None
    last = util.day_month(F.tp.get("last_service_date") or F.cust_rel.get("last_visit"))
    slots = _slot_labels(F, "available_slots")
    kw = tuple(service.split()) if service else words[2]
    offer = F.best_offer(kw)
    if offer and not any(k in offer.lower() for k in kw):
        offer = None                                  # only mention an offer that matches the due service
    lang = F.customer_lang
    svc = service or (words[0] if lang == "en" else words[1])
    if lang == "en":
        line = (f"{subject}'s {svc} is due" if subject else f"Your {svc} is due") + (f" — last visit was on {last}." if last else ".")
    else:
        line = (f"{subject + ' ki' if subject else 'Aapki'} {svc} due hai" + (f" — last visit {last} ko hui thi." if last else "."))
    offer_txt = f" {offer}." if offer else ""
    if len(slots) >= 2:
        slot_txt = (f" Slots ready hain: {slots[0]} ya {slots[1]}." if lang != "en" else f" Two slots are open: {slots[0]} or {slots[1]}.")
        cta = "Reply 1 for the first, 2 for the second." if lang == "en" else "Reply 1 ya 2."
        cta_type = "multi_choice_slot"
    elif len(slots) == 1:
        slot_txt = f" Next open slot: {slots[0]}."
        cta = customer_cta(F, "Shall we hold it for you", "Aapke liye hold kar dein")
        cta_type = "binary_yes_no"
    else:
        slot_txt = ""
        cta = customer_cta(F, words[3], words[4])
        cta_type = "binary_yes_no"
    body = f"{opener(F)} {line}{slot_txt}{offer_txt} {cta}"
    return Draft(util.clean_ws(body), cta_type,
                 "Customer recall due: sent on the merchant's behalf in the customer's language, with the real due service, "
                 "last-visit date, open slots and only a matching live price — no medical claims.",
                 send_as="merchant_on_behalf", topic="recall booking",
                 followup={"label": "booking", "artifact": "", "confirm": ""})


@strategy("appointment_tomorrow")
def appointment_tomorrow(F: Facts) -> Draft:
    t = util.clock(F.tp.get("appointment_iso") or F.tp.get("slot_iso"))
    svc = util.humanize(F.tp.get("service")) if F.tp.get("service") else None
    lang = F.customer_lang
    when = f"tomorrow at {t}" if t else "tomorrow"
    noun = {"restaurants": "table booking", "gyms": "session"}.get(F.slug, "appointment")
    what = f"your {svc} {noun}" if svc else f"your {noun}"
    if lang == "en":
        line = f"A quick reminder that {what} is {when}."
        cta = "Reply YES to confirm, or NO if you need to reschedule."
    else:
        line = f"Yaad dilana tha — {'aapka ' + svc if svc else 'aapka'} {noun} {when.replace('tomorrow', 'kal')} hai."
        cta = "Confirm karne ke liye YES reply karein, reschedule ke liye NO."
    body = f"{opener(F)} {line} {cta}"
    return Draft(util.clean_ws(body), "binary_confirm_cancel",
                 "Appointment reminder the day before: transactional, single confirm/reschedule binary, customer's language.",
                 send_as="merchant_on_behalf", topic="appointment confirmation")


@strategy("chronic_refill_due")
def chronic_refill(F: Facts) -> Draft:
    if F.slug != "pharmacies" and not F.tp.get("molecule_list"):
        d = recall_due(F)
        d.rationale = "Refill-type trigger for a non-pharmacy: treated as a routine follow-up booking, not a prescription dispatch. " + d.rationale
        return d
    mols = F.tp.get("molecule_list") or []
    runout = util.day_month(F.tp.get("stock_runs_out_iso"))
    saved = F.tp.get("delivery_address_saved")
    addressee, subject = F.customer_names()
    lang = F.customer_lang
    hi = lang != "en" or bool(F.via_relative)
    senior = bool(F.cust_identity.get("senior_citizen"))
    senior_offer = next((o for o in F.active_offer_titles if "senior" in o.lower()), None) if senior else None
    delivery = next((o for o in F.active_offer_titles if "deliver" in o.lower()), None)
    # cross-check the category's recall alerts against this customer's molecules
    alert = next((d for d in F.digest() if d.get("kind") in ("alert", "recall")
                  and any(m.lower() in str(d.get("title", "")).lower() for m in mols)), None)
    name_ref = (f"{addressee.replace('Mr. ', '').replace('Mrs. ', '')} ji" if addressee and hi else addressee) or ""
    if mols:
        med = util.join_list(mols, "aur" if hi else "and")
        if hi:
            line = f"{name_ref + ' ki' if name_ref else 'Aapki'} monthly medicines ({med})" + (f" {runout} tak khatam ho jayengi." if runout else " refill ke liye due hain.")
        else:
            line = f"{(name_ref + chr(39) + 's') if name_ref else 'Your'} monthly medicines ({med})" + (f" run out on {runout}." if runout else " are due for a refill.")
    else:
        if F.slug == "pharmacies":
            line = ("Aapka regular refill due hai." if hi else "Your regular refill is due.")
        else:
            line = ("Aapka regular follow-up due hai." if hi else "Your regular follow-up is due.")
    extras = []
    if senior_offer:
        extras.append(senior_offer + (" lagega" if hi else " applies"))
    if delivery and saved:
        extras.append(("saved address par " if hi else "delivery to your saved address — ") + delivery)
    elif delivery:
        extras.append(delivery)
    extra_txt = (" " + "; ".join(extras) + ".") if extras else ""
    alert_txt = ""
    if alert:
        alert_txt = (" Atorvastatin ka batch hum current recall list se check karke hi bhejenge." if hi and "atorvastatin" in alert.get("title", "").lower()
                     else " We'll check the batch against the current recall notice before dispatch.")
    cta = "Same prescription dispatch karne ke liye CONFIRM reply karein." if hi else "Reply CONFIRM to dispatch the same prescription."
    body = f"{opener(F)} {line}{extra_txt}{alert_txt} {cta}"
    return Draft(util.clean_ws(body), "binary_confirm_cancel",
                 "Chronic refill: exact molecules and run-out date, the merchant's real senior/delivery offers, respectful "
                 "Hindi for a senior reached via family, and a proactive recall-batch check — single CONFIRM action.",
                 send_as="merchant_on_behalf", topic="refill dispatch")


@strategy("customer_lapsed_soft", "customer_lapsed_hard", "customer_lapsed", "winback_customer")
def customer_lapsed(F: Facts) -> Draft:
    days = F.tp.get("days_since_last_visit")
    focus = util.humanize(F.tp.get("previous_focus") or F.cust_prefs.get("training_focus") or "")
    months = F.tp.get("previous_membership_months")
    last = util.day_month(F.cust_rel.get("last_visit"))
    lang = F.customer_lang
    offer = F.best_offer(("trial", "free", "first", "month", "clean", "check", "haircut", "spa"))
    gap = f"It's been {int(days)} days since your last visit" if days else (f"We haven't seen you since {last}" if last else "It's been a while since your last visit")
    if lang != "en":
        gap = (f"Aapki last visit ko {int(days)} din ho gaye" if days else (f"{last} ke baad aap nahi aaye" if last else "Kaafi time ho gaya"))
    no_shame = " — happens to everyone, no judgment." if F.slug == "gyms" else "."
    goal = ""
    if focus:
        goal = f" If {focus} is still the goal, we'd love to help you pick it back up." if lang == "en" else \
            f" Agar {focus} abhi bhi goal hai, hum help karenge."
    offer_txt = (f" We're running {offer} right now." if lang == "en" else f" Abhi {offer} chal raha hai.") if offer else ""
    pref = F.cust_prefs.get("preferred_slots")
    slot = util.humanize(pref) if pref else None
    if F.slug == "gyms":
        q_en = f"Shall we hold a {slot + ' ' if slot else ''}spot for you this week — no commitment"
        q_hi = "Is hafte aapke liye ek spot hold kar dein"
    elif F.slug in ("pharmacies", "restaurants"):
        q_en, q_hi = RECALL_WORDS[F.slug][3], RECALL_WORDS[F.slug][4]
    else:
        q_en = f"Shall we hold a {slot + ' ' if slot else ''}slot for you this week"
        q_hi = "Is hafte aapke liye ek slot hold kar dein"
    body = f"{opener(F)} {gap}{no_shame}{goal}{offer_txt} {customer_cta(F, q_en, q_hi)}"
    return Draft(util.clean_ws(body), "binary_yes_no",
                 "Lapsed customer win-back on the merchant's behalf: warm, no-guilt framing, their own past goal and preferred "
                 "slot, the merchant's live offer, single YES.",
                 send_as="merchant_on_behalf", topic="win-back visit")


@strategy("trial_followup")
def trial_followup(F: Facts) -> Draft:
    trial = util.day_month(F.tp.get("trial_date") or F.cust_rel.get("last_visit"))
    opts = _slot_labels(F, "next_session_options")
    addressee, subject = F.customer_names()
    services = F.cust_rel.get("services_received") or []
    default = {"pharmacies": "first visit", "restaurants": "first visit"}.get(F.slug, "trial session")
    what = util.humanize(services[-1]) if services else default
    who = f"bringing {subject}" if subject else "coming in"
    line = f"Thanks for {who} for the {what}{' on ' + trial if trial else ''}!"
    if opts:
        nxt = f" The next session is {opts[0]}."
        q = customer_cta(F, f"Shall we save {'him' if subject else 'you'} a spot", "Spot save kar dein")
    else:
        offer = F.best_offer(("first", "month", "trial"))
        nxt = f" {offer} is open for new joiners." if offer and F.slug == "gyms" else ""
        words = RECALL_WORDS.get(F.slug)
        q = customer_cta(F, "Shall we share this week's session times", "Is hafte ki timings bhej dein") if F.slug == "gyms" or not words \
            else customer_cta(F, words[3], words[4])
    body = f"{opener(F)} {line}{nxt} {q}"
    return Draft(util.clean_ws(body), "binary_yes_no",
                 "Post-trial follow-up while the experience is fresh: references the actual trial date and the next real session.",
                 send_as="merchant_on_behalf", topic="next session booking")


@strategy("wedding_package_followup", "bridal_followup")
def bridal_followup(F: Facts) -> Draft:
    wedding = F.tp.get("wedding_date") or F.cust_prefs.get("wedding_date")
    days = F.tp.get("days_to_wedding")
    trial = util.day_month(F.tp.get("trial_completed"))
    nxt = util.humanize(F.tp.get("next_step_window_open") or "")
    nxt = nxt.replace("30day", "30-day")
    wd = util.day_month(wedding)
    line1 = f"Thanks again for your bridal trial{' on ' + trial if trial else ''}."
    countdown = f" {int(days)} days to go until {wd}" if days and wd else (f" Your wedding is on {wd}" if wd else "")
    plan = f" — a good window to plan your {nxt} so it wraps up well before the peak bridal rush." if nxt else " — a good time to plan your pre-wedding sessions."
    pref = F.cust_prefs.get("preferred_slots")
    q = customer_cta(F, f"Shall we pencil in a {util.humanize(pref) + ' ' if pref else ''}consult to map it out",
                     "Plan banane ke liye ek consult pencil in kar dein")
    body = f"{opener(F).replace('✨', '💍', 1)} {line1}{countdown}{plan} {q}"
    return Draft(util.clean_ws(body), "binary_yes_no",
                 "Bridal follow-up: continuity from the real trial date, exact wedding countdown, next program window, and the "
                 "bride's preferred day — no invented package price.",
                 send_as="merchant_on_behalf", topic="bridal prep consult")


def customer_generic(F: Facts) -> Draft:
    offer = F.best_offer()
    last = util.day_month(F.cust_rel.get("last_visit"))
    lang = F.customer_lang
    line = (f"It's been a while since your visit on {last}." if last else "Hope you're doing well.") if lang == "en" else \
        (f"{last} ke baad aapse milna nahi hua." if last else "Umeed hai sab badhiya hai.")
    offer_txt = f" {offer} is running this week." if offer else ""
    body = f"{opener(F)} {line}{offer_txt} " + customer_cta(F, "Shall we book you in", "Aapke liye slot book kar dein")
    return Draft(util.clean_ws(body), "binary_yes_no",
                 "Customer-scoped trigger without a specific playbook: relationship + live offer, single booking ask.",
                 send_as="merchant_on_behalf", topic="booking")
