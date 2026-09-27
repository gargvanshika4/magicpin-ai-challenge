"""Voice: how a message sounds for a given category / language preference.

Kept deliberately small — the category JSON (voice.tone, vocab, taboos) is
the source of truth; this module only turns it into sentence endings,
greetings and code-mix choices.
"""
from __future__ import annotations

from typing import Optional

from .facts import Facts

CATEGORY_EMOJI = {"dentists": "🦷", "salons": "✨", "restaurants": "🍽️", "gyms": "💪", "pharmacies": "🙏"}


def merchant_cta(F: Facts, question_en: str, question_hi: Optional[str] = None,
                 reply_word: str = "YES") -> str:
    """Single closing CTA for merchant-facing messages.

    Hindi-primary merchants get a natural Hindi-English ending; everyone else
    gets plain English. Always exactly one question + one reply instruction.
    """
    q_en = question_en.rstrip("?. ") + "?"
    if F.merchant_code_mix:
        if question_hi:
            return f"{question_hi.rstrip('?. ')}? Bas {reply_word} reply kar dijiye."
        return f"{q_en} Bas {reply_word} reply kar dijiye."
    return f"{q_en} Reply {reply_word}."


def customer_greeting(F: Facts) -> str:
    addressee, _subject = F.customer_names()
    lang = F.customer_lang
    emoji = CATEGORY_EMOJI.get(F.slug, "")
    if F.via_relative and (lang != "en" or not addressee):
        return "Namaste 🙏"
    if not addressee:
        return "Namaste 🙏" if lang != "en" else "Hi there,"
    if lang == "hi":
        return f"Namaste {addressee} ji 🙏"
    return f"Hi {addressee} {emoji}".strip()


def from_line(F: Facts) -> str:
    """'Karthik from PowerHouse Fitness here.' / 'Dr. Meera's Dental Clinic se.'"""
    lang = F.customer_lang
    who = F.biz
    if F.slug == "dentists":
        base = who
    elif F.owner and F.slug in ("salons", "gyms"):
        base = f"{F.owner} from {who}"
    else:
        base = who
    loc = f", {F.locality}" if F.locality and F.locality.lower() not in who.lower() else ""
    if lang in ("mix", "hi"):
        return f"{who}{loc} se message kar rahe hain."
    return f"{base}{loc} here."


def opener(F: Facts) -> str:
    """Greeting + who-is-writing, e.g. 'Hi Priya 🦷 Dr. Meera's Dental Clinic, Lajpat Nagar here.'"""
    return f"{customer_greeting(F)} {from_line(F)}"


def customer_cta(F: Facts, en: str, hi: Optional[str] = None, reply_word: str = "YES") -> str:
    if F.customer_lang in ("mix", "hi") and hi:
        return f"{hi.rstrip('?. ')}? Reply {reply_word}."
    return f"{en.rstrip('?. ')}? Reply {reply_word}."


def people(F: Facts) -> str:
    return F.nouns[1]
