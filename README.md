# Vera — magicpin merchant assistant (AI Challenge submission)

Vera messages merchants on WhatsApp to improve their Google listing, run campaigns, and reply to their customers on their behalf. This bot decides **what to send next, to whom, and why**. It then writes a short, grounded message with a single call to action (CTA).

## Approach

The core is a **deterministic decision engine**. There is no LLM anywhere in the decision path.

```
context -> Facts (normalized view of category + merchant + trigger + customer)
        -> consent gate (customer-facing only)
        -> strategy for trigger.kind:  decide ONE anchor + ONE action, then write
        -> validator: every number grounded? 1 CTA? no URL / taboo / jargon?
        -> repair (drop unverifiable clause) -> fallback (dominant-signal message)
        -> suppression key + WhatsApp template name/params
```

**Choosing the anchor.** The trigger answers *why now*. The merchant's state answers *what to anchor on*.

- `vera/signals.py` extracts every candidate signal: perf deltas, CTR/calls vs peers, no live offer, stale posts, unverified listing, review themes, lapsed pool, subscription.
- Each candidate is scored as magnitude × weight × mode affinity (problem / positive / neutral).
- The engine takes the argmax, with ties broken by name. It never dumps several signals into one message.

**Strategies.** `vera/strategies.py` has one strategy per trigger kind (27 kinds plus generic fallbacks). Each makes category-aware calls, for example:

- A **Saturday IPL match** gets a recommendation to push delivery, not dine-in, citing the category's own −12% covers data. The Tue–Thu BOGO is saved for weekday matches.
- A **new competitor** gets "don't start a price war": the computed ₹100 gap is stated, and the merchant is pointed at the strength their reviews already praise.
- A **seasonal dip** is reframed as expected, and effort is shifted to retaining current members.
- A **renewal with more than 30 days left** gets no hard sell.
- A **chronic refill** cross-checks the category recall alert for the same molecule.
- **Planning intent** skips re-qualification and delivers a draft built on the merchant's live offer.

**Sparse data.** 75 of the 100 generated triggers have placeholder payloads. For those, strategies fall back to the dominant *real* merchant signal and never invent numbers. They also never frame a problem as good news: a spike or milestone trigger only uses positive facts, or a true round-number threshold.

## API (`app.py`)

| Endpoint | Behaviour |
|---|---|
| `GET /v1/healthz` | Returns status, uptime and per-scope context counts. |
| `GET /v1/metadata` | Returns team and approach info, configured via environment variables. |
| `POST /v1/context` | Versioned and idempotent. A higher version replaces the stored one atomically. The same or a lower version returns **409** `stale_version` with `current_version`, per api-call-examples 1.5. Malformed input returns 400; bodies over 500 KB return 413. |
| `POST /v1/tick` | Returns at most **20** actions per tick. Merchant-facing sends are limited to one per merchant per tick; see Suppression for the other rules. |
| `POST /v1/reply` | Returns `send`, `wait` or `end`. See the reply policy below. |
| `POST /v1/teardown` | Wipes all state. |

`/v1/tick` treats the judge's `available_triggers` as authoritative. `expires_at` does not veto a trigger the judge lists as active, because the local simulator sends wall-clock `now`.

**Reply policy** (checked in this priority order):

1. **Auto-reply.** Detected from canned phrasing, or from identical text repeated by the same merchant. The response sequence is: one short flag for the owner, then wait, then end. This is counted per merchant across conversations, so rotating `conversation_id`s can't defeat it.
2. **Opt-out.** The conversation ends and the merchant is suppressed.
3. **Hostile.** One apology line is sent and proactive sends are paused.
4. **Off-topic** (GST, loans, and similar). Declined in one line and redirected to the open thread.
5. **Commit.** The drafted artifact is delivered immediately, then confirmed.
6. **Later.** The bot waits: 30 minutes, 4 hours or 24 hours depending on the reply.
7. **Decline.** The conversation ends.
8. **Question.** Answered from context (for example the renewal amount), or with a preview of the artifact.

The reply language follows the merchant's language on each turn. After 5 bot turns in one conversation, the bot exits gracefully.

## Hallucination prevention

`vera/validate.py` pulls every number out of the message body. Each one must appear somewhere in the four input contexts (including numbers inside strings and dates, and fractions in percent form), or be explicitly registered via `Facts.derive()` (for example a price gap of ₹100, or 150 − 145 = 5).

- If a non-CTA clause fails the check, it is dropped. If the whole message fails, the strategy falls back.
- Catalog offers are only ever *suggested* ("switch on 'Haircut @ ₹99' from the catalog"). They are never claimed as live.
- Things the case studies invent are never used: affected-customer counts, basket totals, office names, freebies.

## Suppression

- Every message uses the trigger's `suppression_key`, or a deterministic `kind:merchant:customer:trigger` key if none is given.
- A key already sent with the **same trigger fingerprint** (content + version) is skipped. A new version with changed content is allowed again, so adaptive injections still get through.
- Merchants who are opted out, muted or in a wait window are skipped.
- A merchant who hasn't replied to an earlier nudge this session only receives urgent sends (urgency ≥ 3).
- A merchant with 3 or more unanswered nudges in their stored history only receives sends with urgency ≥ 4.

## Determinism

The engine uses no randomness, no clock reads in decision logic, and no network calls. All sorts are stable with explicit tie-breaks, and conversation IDs are readable and deterministic (`conv_<who>_<kind>`). Timestamps only appear in infrastructure fields such as `stored_at`. `compose()` is a pure function, and the tests call it twice per trigger and compare the outputs.

## Customer-facing sends and consent

Customer messages are sent as `send_as: merchant_on_behalf`. Before sending, the bot requires `consent.opted_in_at`, a non-empty consent scope, and `reminder_opt_in` not set to false. If any is missing, the message is re-routed to the merchant, suggesting an opt-in prompt instead of contacting the customer.

The message honours the customer's language preference (English, Hindi-English mix, or respectful Hindi), and addresses a parent or family member when the customer is reached through one.

## Run locally

```bash
pip install -r requirements-dev.txt
python3 dataset/generate_dataset.py --seed-dir dataset --out expanded   # 50 merchants / 200 customers / 100 triggers / 30 pairs
uvicorn app:app --host 0.0.0.0 --port 8080 --workers 1
python3 -m pytest -q                                                     # 36 tests
python3 scripts/make_submission.py --data expanded --out submission.jsonl
```

**Judge simulator.** `scripts/run_local_judge.py` runs the official `judge_simulator.py` scenarios without editing that file:

```bash
BOT_URL=http://localhost:8080 python3 scripts/run_local_judge.py all
LLM_PROVIDER=anthropic LLM_API_KEY=sk-... python3 scripts/run_local_judge.py full_evaluation
```

- **Without a key**, the rule-checked scenarios (warmup, auto-reply, intent, hostile) run for real, and message scores use the simulator's heuristic fallback.
- **With a key**, the simulator's LLM scorer is used.

## Deploy

The service listens on `0.0.0.0:$PORT` and **must run with 1 worker**, because state lives in the process. Options:

- **Docker:** `docker build -t vera . && docker run -p 8080:8080 vera`
- **Render / Railway / Fly / Heroku:** use the `Procfile`.
- **Restart safety:** optionally set `VERA_STATE_FILE` to snapshot contexts so a restart mid-test doesn't lose state. The file is deleted on teardown.

Submit the resulting `https://<host>` as your bot URL.

## Tradeoffs

- **Rules over an LLM.** Determinism, the 30-second budget, and zero fabrication were worth more than stylistic variety. The cost is less variety in copy on unseen trigger kinds, which go to the generic dominant-signal message. An LLM paraphrase step could be added *behind* the validator later.
- **Restraint.** One merchant-facing message per merchant per tick, and low-urgency sends held while a merchant is unresponsive. This trades message volume for quality, as the brief rewards.
- **Same-version re-push returns 409,** following the concrete example over the looser "no-op" wording. State is unchanged either way.
- **No URLs in messages.** The main brief allows them, but the API examples penalise them with −3, so the stricter rule wins.

**What would help most:**

- real appointment slots and a service menu with prices per merchant;
- review counts and ratings on every merchant;
- non-placeholder payloads on the generated triggers.
