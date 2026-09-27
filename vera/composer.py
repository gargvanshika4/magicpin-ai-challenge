"""compose(category, merchant, trigger, customer?) -> decision.

Pipeline:  normalize -> scope & consent gate -> strategy (decide, then write)
           -> validate -> repair / fallback -> suppression key + template.

Pure function: same inputs, same output. No clock, no randomness, no network.
"""
from __future__ import annotations

from typing import Optional

from . import strategies as S
from . import util, validate
from .facts import Facts
from .voice import merchant_cta

CUSTOMER_KINDS = {"recall_due", "appointment_tomorrow", "chronic_refill_due", "customer_lapsed_soft",
                  "customer_lapsed_hard", "trial_followup", "wedding_package_followup", "bridal_followup",
                  "winback_customer", "customer_lapsed"}


KIND_LABEL = {"recall_due": "check-up reminder", "appointment_tomorrow": "appointment reminder",
              "chronic_refill_due": "refill reminder", "trial_followup": "trial follow-up",
              "customer_lapsed_soft": "win-back message", "customer_lapsed_hard": "win-back message",
              "wedding_package_followup": "bridal follow-up"}


def _is_customer_scope(trigger: dict, customer: Optional[dict]) -> bool:
    return trigger.get("scope") == "customer" or bool(trigger.get("customer_id")) or \
        (trigger.get("kind") in CUSTOMER_KINDS)


def consent_ok(F: Facts) -> tuple[bool, str]:
    c = F.customer or {}
    consent = F.cust_consent
    if not c:
        return False, "customer context missing"
    if not consent.get("opted_in_at") or not consent.get("scope"):
        return False, "customer has not opted in to WhatsApp messages from the merchant"
    if F.cust_prefs.get("reminder_opt_in") is False:
        return False, "customer opted out of reminders"
    return True, ""


def _consent_blocked_draft(F: Facts, reason: str) -> S.Draft:
    addressee, subject = F.customer_names()
    who = subject or addressee or f"one of your {F.nouns[1]}"
    kind = KIND_LABEL.get(F.trigger.get("kind"), util.humanize(F.trigger.get("kind") or "follow-up"))
    body = (f"{F.salutation}, {who} is due for a {kind}, but they haven't opted in to WhatsApp "
            f"reminders from {F.biz}, so I won't contact them directly. "
            + merchant_cta(F, "Shall I add a quick opt-in prompt to their next bill instead",
                           "Unke next bill par ek opt-in prompt add kar doon"))
    return S.Draft(util.clean_ws(body), "binary_yes_no",
                   f"Customer-scoped trigger but consent check failed ({reason}); routed to the merchant for an opt-in "
                   f"step instead of messaging the customer.", send_as="vera", topic="customer opt-in")


def _minimal_safe(F: Facts, customer: bool) -> S.Draft:
    if customer:
        body = f"Hi, {F.biz} here. " + ("We'd love to see you again — reply YES and we'll share open slots.")
        return S.Draft(body, "binary_yes_no", "Safe fallback after validation failure.", send_as="merchant_on_behalf",
                       topic="booking")
    body = f"{F.salutation}, " + merchant_cta(F, f"shall I draft this week's Google post for {F.biz}",
                                              "Is hafte ka Google post draft kar doon")
    return S.Draft(body, "binary_yes_no", "Safe fallback after validation failure.", topic="weekly post")


def suppression_key(trigger: dict, merchant: dict, customer: Optional[dict]) -> str:
    if trigger.get("suppression_key"):
        return str(trigger["suppression_key"])
    parts = [str(trigger.get("kind") or "msg"), str(merchant.get("merchant_id") or "m")]
    if customer:
        parts.append(str(customer.get("customer_id")))
    parts.append(str(trigger.get("id") or ""))
    return ":".join(p for p in parts if p)


def _template(draft: S.Draft, F: Facts, kind: str, is_customer: bool) -> tuple[str, list]:
    prefix = "merchant" if draft.send_as == "merchant_on_behalf" else "vera"
    name = f"{prefix}_{util.slug(kind) or 'update'}_v1"
    sents = util.sentences(draft.body.replace("\n", " "))
    cta = sents[-1] if sents else ""
    if len(sents) >= 2 and "?" in sents[-2] and not "?" in sents[-1]:
        cta = sents[-2] + " " + sents[-1]
        hook = " ".join(sents[:-2])
    else:
        hook = " ".join(sents[:-1])
    addressee = (F.customer_names()[0] or "there") if is_customer else F.salutation
    return name, [addressee, hook.strip(), cta.strip()]


def compose(category: Optional[dict], merchant: Optional[dict], trigger: Optional[dict],
            customer: Optional[dict] = None) -> dict:
    category, merchant, trigger = category or {}, merchant or {}, trigger or {}
    F = Facts(category, merchant, trigger, customer)
    kind = str(trigger.get("kind") or "unknown")
    is_customer = _is_customer_scope(trigger, customer)

    if is_customer:
        ok, reason = consent_ok(F)
        if not ok:
            draft = _consent_blocked_draft(F, reason) if customer else S.merchant_generic(F)
            is_customer = False
        else:
            fn = S.STRATEGIES.get(kind, S.customer_generic)
            draft = fn(F)
    else:
        fn = S.STRATEGIES.get(kind, S.merchant_generic)
        draft = fn(F)
        if draft.send_as != "vera":          # customer strategy fired without a customer
            draft = S.merchant_generic(F)

    # ---- validate -> repair -> fallback chain
    rep = validate.check(draft.body, F, draft.cta)
    if not rep.ok:
        repaired = util.clean_ws(validate.repair(draft.body, F, draft.cta))
        rep2 = validate.check(repaired, F, draft.cta)
        if rep2.ok:
            draft.body = repaired
            draft.rationale += " (validator trimmed an unverifiable clause)"
        else:
            fb = (S.customer_generic(F) if is_customer else S.merchant_generic(F))
            if validate.check(fb.body, F, fb.cta).ok:
                fb.rationale = f"Primary draft failed validation ({'; '.join(rep.problems)}); " + fb.rationale
                draft = fb
            else:
                draft = _minimal_safe(F, is_customer)

    name, params = _template(draft, F, kind, is_customer)
    return {
        "body": draft.body,
        "cta": draft.cta,
        "send_as": draft.send_as,
        "suppression_key": suppression_key(trigger, merchant, customer if is_customer else None),
        "rationale": draft.rationale,
        "template_name": name,
        "template_params": params,
        "topic": draft.topic,
        "followup": draft.followup,
        "strategy": getattr(S.STRATEGIES.get(kind), "__name__", "generic"),
        "customer_scoped": is_customer,
    }
