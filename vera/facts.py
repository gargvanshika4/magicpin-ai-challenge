"""Normalized, read-only view over (category, merchant, trigger, customer).

Every accessor tolerates missing / partial data (the generated dataset has
many sparse merchants and placeholder trigger payloads). Anything the
composer computes (e.g. a gap of ₹100 between two prices) must be registered
via `derive()` so the validator accepts it as grounded.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from . import util

CATEGORY_NOUNS = {
    # slug: (singular business noun, people noun, one-person noun)
    "dentists": ("clinic", "patients", "patient"),
    "salons": ("salon", "clients", "client"),
    "restaurants": ("restaurant", "diners", "diner"),
    "gyms": ("studio", "members", "member"),
    "pharmacies": ("pharmacy", "customers", "customer"),
}

HINDI_BELT_HINT = {"hi"}


def _d(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


def _l(x: Any) -> list:
    return x if isinstance(x, list) else []


class Facts:
    def __init__(self, category: Optional[dict], merchant: Optional[dict],
                 trigger: Optional[dict], customer: Optional[dict] = None):
        self.category = _d(category)
        self.merchant = _d(merchant)
        self.trigger = _d(trigger)
        self.customer = _d(customer) if customer else None
        self._derived: set = set()

        self.identity = _d(self.merchant.get("identity"))
        self.perf = _d(self.merchant.get("performance"))
        self.delta = _d(self.perf.get("delta_7d"))
        self.sub = _d(self.merchant.get("subscription"))
        self.agg = _d(self.merchant.get("customer_aggregate"))
        self.voice = _d(self.category.get("voice"))
        self.peer = _d(self.category.get("peer_stats"))
        self.tp = _d(self.trigger.get("payload"))  # trigger payload

    # ------------------------------------------------------------------ grounding
    def derive(self, *values: Any) -> None:
        """Register numbers the composer computed from grounded inputs."""
        for v in values:
            if v is None:
                continue
            for f in (util.extract_numbers(v) if isinstance(v, str) else [float(v)]):
                self._derived.add(round(abs(f), 1))
                self._derived.add(round(abs(f)))

    def allowed_numbers(self) -> set:
        acc = set()
        for obj in (self.category, self.merchant, self.trigger, self.customer):
            util.grounding_numbers(obj, acc)
        return acc | self._derived

    # ------------------------------------------------------------------ identity
    @property
    def slug(self) -> str:
        return (self.category.get("slug") or self.merchant.get("category_slug") or "").lower()

    @property
    def nouns(self) -> tuple:
        return CATEGORY_NOUNS.get(self.slug, ("business", "customers", "customer"))

    @property
    def biz(self) -> str:
        return self.identity.get("name") or "your business"

    @property
    def owner(self) -> str:
        return util.strip_dr(self.identity.get("owner_first_name") or "")

    @property
    def locality(self) -> str:
        return self.identity.get("locality") or ""

    @property
    def city(self) -> str:
        return self.identity.get("city") or ""

    @property
    def salutation(self) -> str:
        """How Vera addresses the merchant."""
        owner = self.owner
        if self.slug == "dentists":
            return f"Dr. {owner}" if owner else "Doctor"
        return owner or f"{self.biz} team"

    @property
    def merchant_code_mix(self) -> bool:
        """Light Hindi-English mix when Hindi is a primary merchant language
        and the category voice allows natural code-mix."""
        langs = [str(l).lower() for l in _l(self.identity.get("languages"))]
        mix_ok = "hindi" in str(self.voice.get("code_mix", "hindi_english_natural"))
        english_primary = "english_primary" in str(self.voice.get("code_mix", ""))
        return "hi" in langs[:2] and mix_ok and not english_primary

    # ------------------------------------------------------------------ offers
    def offers(self, status: str = "active") -> list[dict]:
        return [o for o in _l(self.merchant.get("offers"))
                if isinstance(o, dict) and o.get("status") == status and o.get("title")]

    @property
    def active_offer_titles(self) -> list[str]:
        return [o["title"] for o in self.offers("active")]

    def best_offer(self, keywords: tuple = (), prefer_price: bool = True) -> Optional[str]:
        """Pick the active offer most relevant to keywords (deterministic)."""
        titles = self.active_offer_titles
        if not titles:
            return None
        def score(t: str) -> tuple:
            tl = t.lower()
            kw = sum(1 for k in keywords if k in tl)
            priced = 1 if ("₹" in t and prefer_price) else 0
            return (kw, priced, -titles.index(t))
        return max(titles, key=score)

    def catalog_offer(self, keywords: tuple = (), audience: Optional[str] = None) -> Optional[str]:
        """A catalog offer to *suggest* (never claimed as already live)."""
        cat = [o for o in _l(self.category.get("offer_catalog")) if isinstance(o, dict) and o.get("title")]
        if audience:
            cat = [o for o in cat if o.get("audience") in (audience, "all")] or cat
        if not cat:
            return None
        def score(o: dict) -> tuple:
            tl = o["title"].lower()
            kw = sum(1 for k in keywords if k in tl)
            priced = 1 if o.get("type") == "service_at_price" else 0
            return (kw, priced, -cat.index(o))
        return max(cat, key=score)["title"]

    # ------------------------------------------------------------------ digest
    def digest(self) -> list[dict]:
        return [d for d in _l(self.category.get("digest")) if isinstance(d, dict)]

    def digest_item(self, item_id: Optional[str]) -> Optional[dict]:
        if not item_id:
            return None
        for d in self.digest():
            if d.get("id") == item_id:
                return d
        return None

    def pick_digest(self, kinds: tuple) -> Optional[dict]:
        """Most merchant-relevant digest item of the given kinds."""
        items = [d for d in self.digest() if d.get("kind") in kinds] or []
        if not items:
            return None
        sig_text = " ".join(self.signal_names() + [str(k) for k in self.agg.keys()]).lower()
        def score(d: dict) -> tuple:
            seg = str(d.get("patient_segment", "")).lower()
            rel = 2 if seg and seg.rstrip("s") in sig_text else 0
            text = (str(d.get("title", "")) + " " + str(d.get("summary", ""))).lower()
            rel += sum(1 for t in self.active_offer_titles if t.split("@")[0].strip().lower()[:8] in text)
            return (rel, kinds.index(d.get("kind")) * -1, bool(d.get("source")), -items.index(d))
        return max(items, key=score)

    # ------------------------------------------------------------------ signals
    def signal_names(self) -> list[str]:
        return [str(s).split(":")[0] for s in _l(self.merchant.get("signals"))]

    def signal_value(self, name: str) -> Optional[str]:
        for s in _l(self.merchant.get("signals")):
            s = str(s)
            if s.split(":")[0] == name and ":" in s:
                return s.split(":", 1)[1]
        return None

    def has_signal(self, name: str) -> bool:
        return name in self.signal_names()

    def review_themes(self, sentiment: Optional[str] = None) -> list[dict]:
        themes = [t for t in _l(self.merchant.get("review_themes")) if isinstance(t, dict)]
        if sentiment:
            themes = [t for t in themes if t.get("sentiment") == sentiment]
        return sorted(themes, key=lambda t: (-(t.get("occurrences_30d") or 0), str(t.get("theme"))))

    # ------------------------------------------------------------------ history
    def history(self) -> list[dict]:
        return [h for h in _l(self.merchant.get("conversation_history")) if isinstance(h, dict)]

    def last_from(self, who: str) -> Optional[dict]:
        for h in reversed(self.history()):
            if h.get("from") == who:
                return h
        return None

    def merchant_engaged_recently(self) -> bool:
        last_m = self.last_from("merchant")
        if last_m and str(last_m.get("engagement", "")).startswith("intent"):
            return True
        return any(s.startswith("engaged_in_last") for s in self.signal_names())

    def unanswered_vera_streak(self) -> int:
        n = 0
        for h in reversed(self.history()):
            if h.get("from") == "vera" and h.get("engagement") == "merchant_no_reply":
                n += 1
            elif h.get("from") == "merchant" or h.get("engagement") == "merchant_replied":
                break
        return n

    # ------------------------------------------------------------------ performance
    def metric(self, name: str) -> Optional[float]:
        v = self.perf.get(name)
        return float(v) if isinstance(v, (int, float)) else None

    def delta_of(self, name: str) -> Optional[float]:
        v = self.delta.get(f"{name}_pct")
        return float(v) if isinstance(v, (int, float)) else None

    def peer_avg(self, name: str) -> Optional[float]:
        for key in (f"avg_{name}_30d", f"avg_{name}"):
            v = self.peer.get(key)
            if isinstance(v, (int, float)):
                return float(v)
        return None

    # ------------------------------------------------------------------ customer
    @property
    def cust_identity(self) -> dict:
        return _d((self.customer or {}).get("identity"))

    @property
    def cust_rel(self) -> dict:
        return _d((self.customer or {}).get("relationship"))

    @property
    def cust_prefs(self) -> dict:
        return _d((self.customer or {}).get("preferences"))

    @property
    def cust_consent(self) -> dict:
        return _d((self.customer or {}).get("consent"))

    def customer_names(self) -> tuple[str, Optional[str]]:
        """Returns (addressee, subject). 'Aanya (parent: Sneha)' -> ('Sneha', 'Aanya')."""
        raw = str(self.cust_identity.get("name") or "").strip()
        m = re.match(r"^(.*?)\s*\(parent:\s*([^)]+)\)", raw)
        if m:
            return m.group(2).strip(), m.group(1).strip()
        if raw.startswith("(") or not raw:
            return "", None
        return raw, None

    @property
    def customer_lang(self) -> str:
        """'mix' (Hindi-English), 'hi' (Roman Hindi leaning) or 'en'."""
        pref = str(self.cust_identity.get("language_pref") or "en").lower()
        if pref in ("hi", "hindi"):
            return "hi"
        if pref.startswith("hi") and "mix" in pref:
            return "mix"
        return "en"

    @property
    def via_relative(self) -> Optional[str]:
        ch = str(self.cust_prefs.get("channel") or "")
        m = re.match(r"whatsapp_via_(\w+)", ch)
        return m.group(1) if m else None

    def preferred_slot_text(self) -> Optional[str]:
        raw = self.cust_prefs.get("preferred_slots")
        return util.humanize(raw).replace("weekday", "weekday") if raw else None
