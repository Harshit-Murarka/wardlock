# Wardlock — Technical Spec (narrowed scope) · v1.7 · Oct 9, 2026

> v1.7 changes:
> - §5 #10: the auto-approve limit covers the session's running total of automatic refunds, which stops salami slicing.
> - §6 steps 9–10: approving a hold re-checks with fresh data, and compat refunds mark their handle as used (both built in P5).
>
> v1.6 changes:
> - §5: the engine as built. The reason check is a separate escalate-only step; new compat code `INVALID_AMOUNT`; stricter combination rules; the limit has a currency; inclusive 180-day window.
>
> v1.5 changes:
> - §0: the ten invariants now live here, the single tracked copy that code comments refer to.
>
> v1.4 changes:
> - §4: handle rules as built. In-flight (reserved) amounts reduce the remaining refundable; re-mint rules; atomic reserve.
> - §10: `/sessions` error behaviour (503 when no key is configured).
>
> v1.3 changes:
> - §7: seeding details checked against the Orders v2 OpenAPI spec. Seeding is resumable with a state file, and has `--restore-db`. It pays with a sandbox test card by default (single-step create, no approvals), with browser approval as a fallback.
> - §9: as built, adding `orders.status`/`placed_at`, `decisions.capture_id`, `holds.created_at` and `handles.label`/`goodwill_pct`.
>
> v1.2 changes:
> - §6: 409 and 429 are retried, never BLOCKed. 401/403 and unreadable 2xx responses → HOLD. Adds the error-to-verdict table.
> - §7: PayPal facts checked against PayPal's OpenAPI spec (field limits, 200 on replay, refund statuses, zero-decimal currencies).
>
> v1.1 changes:
> - §3.1/§4: one goodwill handle per policy percentage.
> - §5: limits live only in `policy.yaml`.
> - §8: reconciliation also matches `custom_id`, so Wardlock's own refunds don't raise false alerts.
> - §9/§10: sessions store the untrusted ticket body.
> - §12: ticket replay page.
> - §13: tests T20–T21.

> Items marked **[VERIFY]** were not confirmed against PayPal's docs. Confirm them in the sandbox before relying on them, and leave a `# VERIFY:` comment in code until you have.

## 0. Invariants (never break these)

Code comments refer to these as "SPEC §0 invariant N".

1. Only `wardlock/paypal/client.py` talks to PayPal. Nothing else makes HTTP calls to PayPal.
2. `request_refund` never accepts a raw amount. Amounts come only from handles (§4).
3. Money is `Decimal` or integer minor units (cents). Never `float`.
4. Every refund POST sends an explicit `amount`, plus a `PayPal-Request-Id` that was saved to the database *before* the call. PayPal treats an empty refund body as a FULL refund.
5. On any money path, an unknown state or a failed dependency means **HOLD**, never ALLOW (fail closed).
6. The LLM reason check may only escalate (ALLOW → HOLD). It can never approve or unblock.
7. Agent-facing tools never return PayPal capture IDs or accept them. The only exception is the compat tool (§3.4), which runs the full ownership check.
8. Sandbox only. The client refuses any base URL other than `https://api-m.sandbox.paypal.com` unless `ALLOW_LIVE=1` is set.
9. Secrets are never printed, logged or committed. Variable names live in `.env.example`.
10. No check and no test is ever weakened, skipped or deleted just to make tests pass.

## 1. Scope

**In scope**
- Credential isolation: the agent holds only a Wardlock session token, never PayPal credentials.
- Sessions bound to one verified customer (opened by trusted orchestrator code).
- Refund *handles*: the agent selects from pre-computed, session-scoped refund options; Wardlock computes the amount.
- Decision engine (pure, deterministic) with reason codes; verdicts ALLOW / HOLD / BLOCK.
- Executor with idempotent PayPal refunds and fail-closed retries.
- Holds approved or denied by the merchant in the console.
- LLM reason check (escalation only).
- Webhook verification and reconciliation (detects refunds Wardlock did not make).
- Compat tool that accepts raw `capture_id` + `amount` but still enforces ownership.
- Demo support agent (LLM) and evaluation harness.
- Admin console (React + AG Grid).

**Out of scope (do not build unless everything else is done)**
- Anomaly scoring, generic threshold rule engines, buyer-side spending mandates, disputes, Telegram approvals, multi-merchant support.

## 2. Actors and trust

| Actor | Trust | Holds |
|---|---|---|
| Orchestrator (our code) | Trusted | `WARDLOCK_SERVICE_KEY` |
| Support agent (LLM) | **Untrusted output** | One session token |
| Wardlock | Trusted | PayPal client ID/secret |
| Merchant admin | Trusted | Console password |
| PayPal sandbox | External | — |

**Trusted inputs:** ticket *metadata* (verified customer ID from the helpdesk), the merchant's `orders` table, PayPal lookups made by Wardlock, `policy.yaml`.
**Untrusted inputs:** ticket *body*, attachments, quoted emails, and anything the agent says or passes.

Ticket files therefore have two parts:
```json
{
  "id": "T-0001",
  "metadata": { "verified_customer_id": "cust_ana", "channel": "email" },
  "body": "Hi, my bike bell arrived scratched ..."
}
```

## 3. Agent-facing MCP tools

All tools require `Authorization: Bearer <session_token>`. Tool annotations: reads `readOnlyHint: true`; `request_refund` and `request_refund_raw` `destructiveHint: true`.

### 3.1 `get_session_context()`
Returns the session customer's first name and their orders. **Never returns PayPal IDs.**
```json
{
  "customer_first_name": "Ana",
  "orders": [{
    "order_ref": "A-1001",
    "placed_at": "2026-09-20",
    "status": "DELIVERED",
    "refund_options": [
      {"handle": "h_3kQ...", "kind": "LINE_ITEM",      "label": "Bike bell x1",        "amount": "4.00",  "currency": "USD"},
      {"handle": "h_9Zt...", "kind": "LINE_ITEM",      "label": "Bottle cage x2",      "amount": "15.98", "currency": "USD"},
      {"handle": "h_Ab1...", "kind": "SHIPPING",       "label": "Shipping",            "amount": "6.50",  "currency": "USD"},
      {"handle": "h_Pq7...", "kind": "GOODWILL_PCT",   "label": "10% goodwill",        "amount": "2.00",  "currency": "USD"},
      {"handle": "h_Rw4...", "kind": "GOODWILL_PCT",   "label": "25% goodwill",        "amount": "5.00",  "currency": "USD"},
      {"handle": "h_Ff2...", "kind": "FULL_REMAINING", "label": "Full remaining refund","amount": "26.48", "currency": "USD"}
    ]
  }]
}
```

### 3.2 `request_refund(refund_handles: list[str], reason_code: ReasonCode, note_to_customer: str)`
- `reason_code` ∈ `DAMAGED | WRONG_ITEM | NOT_RECEIVED | LATE_DELIVERY | GOODWILL`.
- `note_to_customer` ≤ 200 chars (sent as PayPal `note_to_payer`).
- **No amount parameter exists.**

Returns:
```json
{"status": "ALLOWED|HELD|BLOCKED", "amount": "4.00", "currency": "USD",
 "refund_ref": "rf_...", "hold_id": null,
 "reason_codes": [], "message_for_agent": "Refund of $4.00 completed."}
```

### 3.3 `get_refund_status(ref: str)`
Accepts a `refund_ref` or `hold_id` from this session only.

### 3.4 `request_refund_raw(capture_id: str, amount: str, currency: str, reason_code: ReasonCode)` — compat mode
Exists to show the drop-in story and to demonstrate that raw inputs are contained. Runs the same engine plus: capture must belong to the session customer (`NOT_OWNER` → BLOCK), amount required (`MISSING_AMOUNT` → BLOCK), amount must equal exactly one value derivable for that capture (any single handle amount, or full remaining) — otherwise `AMOUNT_NOT_DERIVABLE` → HOLD.

## 4. Handles

Table `handles`: `handle_id` (`"h_" + secrets.token_urlsafe(16)`), `session_id`, `order_id`, `capture_id`, `kind`, `item_ref`, `amount_minor` (int), `currency`, `state` (`AVAILABLE | RESERVED | CONSUMED | EXPIRED`), `created_at`.

Rules:
- Minted only by `get_session_context`, only for orders whose `customer_id` equals the session's customer.
- Valid only in the session that minted it; expire with the session.
- `RESERVED` while a refund is executing or held; `CONSUMED` after a successful refund; back to `AVAILABLE` if a hold is denied or times out.
- One request = one capture. `FULL_REMAINING` cannot be combined with other handles. At most one `GOODWILL_PCT` per request.
- Amounts: `LINE_ITEM` = unit_amount × quantity; `SHIPPING` = breakdown shipping; `GOODWILL_PCT` = percentage (from policy) of item total, rounded half-up to minor units; `FULL_REMAINING` = captured − already refunded (ledger).
- One `GOODWILL_PCT` handle is minted per value in policy `goodwill_percentages` (default 10 and 25 → two handles per order). Example: item total 19.98 → 10% = 2.00, 25% = 5.00 (4.995 rounded half-up).
- Re-minting after a refund must reflect the new remaining refundable amount.

As built in P3 (`wardlock/handles.py`, `wardlock/sessions.py`):
- **Remaining refundable** = captured − ledger refunds (all except `FAILED`/`CANCELLED`) − amounts of **RESERVED** handles on the same capture. The reserved ones are in flight, e.g. held. Without this, two held refunds could add up to more than the capture.
- **Each mint** (`get_session_context` call):
  - first expires the session's older AVAILABLE handles, so no stale amount survives;
  - never re-offers a line item or shipping that is RESERVED or CONSUMED for that order in *any* session;
  - offers no goodwill once any goodwill on the order is reserved or consumed (at most one gesture per order);
  - drops options above the remaining amount, and goodwill that rounds to 0.
  - `FULL_REMAINING` shows the current remaining amount, and is omitted at 0.
- **Reserving** is one conditional `UPDATE … WHERE state=AVAILABLE AND session_id=… RETURNING`. It is all or nothing: on a shortfall, exactly the rows it changed are reverted, and it raises `HandleConflict`. Concurrent requests can't double-spend a handle. Consume is all or nothing too.
- **Session end:** AVAILABLE handles expire, while RESERVED ones wait for their hold. A release after the session ended goes to EXPIRED, not AVAILABLE. Expiry is applied lazily when the session is next resolved.
- **Duplicate handle IDs** in one request collapse to one. **P4 must not sum duplicates**: it should compute amounts over unique handles only.

## 5. Decision engine (pure function, no I/O)

```python
def decide(req: RefundRequest, ctx: SessionCtx, ledger: LedgerView,
           policy: Policy, reason_check: ReasonCheck | None) -> Decision
```

Checks run in order; collect **all** reason codes that can be evaluated (for explainability).

| # | Check | Fail verdict | Reason code |
|---|---|---|---|
| 1 | Session valid and not expired | BLOCK | `SESSION_INVALID` |
| 2 | ≥ 1 handle and every handle exists | BLOCK | `UNKNOWN_HANDLE` |
| 3 | Every handle belongs to this session | BLOCK | `HANDLE_OTHER_SESSION` |
| 4 | No handle is RESERVED/CONSUMED/EXPIRED | BLOCK | `HANDLE_ALREADY_USED` |
| 5 | Same capture; combination rules (§4) | BLOCK | `INVALID_COMBINATION` |
| 6 | Order's customer == session customer (re-read from `orders`) | BLOCK | `NOT_OWNER` |
| 7 | Amount ≤ remaining refundable (ledger) | BLOCK | `EXCEEDS_REFUNDABLE` |
| 8 | Capture age ≤ 180 days | BLOCK | `REFUND_WINDOW_EXPIRED` |
| 9 | Reason allows these handle kinds (policy matrix) | HOLD | `REASON_KIND_MISMATCH` |
| 10 | Amount + the session's earlier **automatic** refunds ≤ policy `auto_approve_limit` | HOLD | `OVER_AUTO_LIMIT` |
| 11 | Successful refunds in session < `max_refunds_per_session` | HOLD | `SESSION_VELOCITY` |
| 12 | Reason check says consistent (only if verdict would be ALLOW) | HOLD | `REASON_NOT_SUPPORTED_BY_TICKET` |
| 13 | Any dependency error while gathering inputs | HOLD | `DEPENDENCY_UNAVAILABLE` |

Compat-only: `NOT_OWNER` (BLOCK), `MISSING_AMOUNT` (BLOCK), `AMOUNT_NOT_DERIVABLE` (HOLD).

**Verdict:** BLOCK if any BLOCK code; else HOLD if any HOLD code; else ALLOW.

Default `policy.yaml`. It is the **only** source for these limits. They are not environment variables, so every decision records exactly which `policy_version` it used.
```yaml
version: 1
currency: USD
auto_approve_limit: "50.00"
max_refunds_per_session: 2
goodwill_percentages: [10, 25]
reason_allows:
  DAMAGED:       [LINE_ITEM, GOODWILL_PCT]
  WRONG_ITEM:    [LINE_ITEM, SHIPPING]
  NOT_RECEIVED:  [FULL_REMAINING]
  LATE_DELIVERY: [SHIPPING, GOODWILL_PCT]
  GOODWILL:      [GOODWILL_PCT]
```

**As built in P4 (`wardlock/engine/`).** Pure: no database, no network, no clock reads. The caller passes `now` and every fact. A test runs it in a fresh interpreter to check it imports nothing that can do I/O. Ambiguities in the table above were resolved like this:
1. **The reason check (#12) is a separate step,** because an LLM call is I/O.
   - `decide()` returns `needs_reason_check=True` for an ALLOW.
   - `apply_reason_check(decision, consistent)` can **only** turn ALLOW into HOLD. Anything but exactly `True` (including `None` for a timeout) holds. It never relaxes a BLOCK or a HOLD (invariant 6).
   - The executor must not run a decision that still needs the check.
2. **Combination rules (#5)** also BLOCK `INVALID_COMBINATION` for the same handle twice (it would double the amount), more than 10 handles, mixed currencies, and any raw amount or capture sent to `request_refund` (invariant 2).
3. **New compat code `INVALID_AMOUNT` (BLOCK)** for an amount that is ≤ 0, not exact in the currency (e.g. 4.001 USD), or in a currency other than the capture's. A HOLD would let a merchant approve a malformed amount.
4. **Compat with a missing or unknown capture → BLOCK `NOT_OWNER`,** so the existence of other captures is never revealed. A compat decision never records another customer's capture as its target.
5. **Compat reason check (#9):** the raw amount maps to the kinds that produce exactly that amount, via `OrderView.derivable`. One allowed kind is enough.
6. **`auto_approve_limit` covers the whole session, in `policy.currency` (USD).**
   - The limit applies to the amount **plus** everything already refunded automatically (verdict ALLOW) in this session (`SessionView.auto_refunded_minor`).
   - This stops "salami slicing": two 49.99 refunds would each pass a per-refund check, but together they are 99.98.
   - Refunds a person approved from a hold don't count.
   - The field has no default, so the caller can't forget it.
   - A refund in any other currency is held, and amounts are never compared across currencies.
7. **The window (#8) is inclusive:** ≤ 180 days. It's a constant, because it's PayPal's rule.
8. **Missing order data for a known handle → HOLD `DEPENDENCY_UNAVAILABLE` (#13).** As a last guard, an otherwise clean decision that lacks an amount or a capture also holds.
9. **Codes are listed in table order,** with the compat codes in their natural place: `MISSING_AMOUNT`/`INVALID_AMOUNT` after `NOT_OWNER`, and `AMOUNT_NOT_DERIVABLE` before #13. Each code has a plain-words explanation in `engine/codes.py`.

## 6. Executor

1. On ALLOW: write `decisions` row with `idempotency_key = uuid4()` **before** calling PayPal; reserve handles.
2. `POST /v2/payments/captures/{capture_id}/refund` with headers `Authorization`, `PayPal-Request-Id: <idempotency_key>`, `Prefer: return=representation`; body:
   ```json
   {"amount": {"value": "4.00", "currency_code": "USD"},
    "note_to_payer": "<note>", "custom_id": "<decision_id>"}
   ```
3. 2xx → write `ledger` row (store PayPal refund id), consume handles, return ALLOWED.
4. Timeout, network error, 5xx, **409** (a previous call is still in progress) or **429** → retry up to 3 times with backoff (0.5 s, 1 s, 2 s) using the **same** `PayPal-Request-Id`. If it is still failing → HOLD with `DEPENDENCY_UNAVAILABLE`. The frozen request is kept, and approving it later reuses the same key, so it cannot double-refund. A 2xx with an unreadable body also → HOLD, because the outcome is unknown.
5. **401/403** → HOLD `DEPENDENCY_UNAVAILABLE`. These mean Wardlock's credentials or permissions are wrong, not the request, and PayPal processed nothing. On a 401 the client first refreshes the token once.
6. Other 4xx (400, 404, 422, …) → BLOCK, store PayPal's error `name` and `details[].issue` codes, release handles. **409 must never land here:** releasing handles while PayPal is still processing would let the agent request the same refund again under a new key.

| Client raises (`wardlock/paypal/errors.py`) | Executor verdict |
|---|---|
| `PayPalUnavailable` (retries exhausted, or outcome unknown) | HOLD `DEPENDENCY_UNAVAILABLE` |
| `PayPalAuthError` (401/403 or token failure) | HOLD `DEPENDENCY_UNAVAILABLE` |
| `PayPalRequestError` (other 4xx) | BLOCK |
7. On HOLD: `holds` row with the frozen request (`capture_id`, `amount`, `currency`, `idempotency_key`, `note`); handles RESERVED.
8. Approve (admin) → execute the frozen request exactly. Deny or timeout (`HOLD_TIMEOUT_MINUTES`) → release handles, mark hold.
9. **Approve re-checks before executing (to build in P5).**
   - The executor rebuilds the facts from **fresh** data and re-runs the engine. If any BLOCK code now appears (session, handle state, ownership, remaining amount, 180-day window), the approval is refused and the hold is marked so. A hold can sit for minutes, and things change in that time.
   - A hold whose codes include `DEPENDENCY_UNAVAILABLE` because order data was missing can't be approved until a re-check passes.
   - The frozen amount and idempotency key never change. The re-check can only stop an approval, never alter one.
10. **Compat refunds mark the matching handle as used (to build in P5).** A raw 4.00 that matches the bell must reserve and then consume the bell's handle. Otherwise the bell would be offered again, and could be refunded twice.

## 7. PayPal API usage (sandbox base `https://api-m.sandbox.paypal.com`)

| Purpose | Call |
|---|---|
| Access token | `POST /v1/oauth2/token` (client credentials, Basic auth). Cache until `expires_in − 60s`. |
| Order details | `GET /v2/checkout/orders/{id}` |
| Capture details | `GET /v2/payments/captures/{id}` |
| Refund | `POST /v2/payments/captures/{id}/refund` |
| Refund details | `GET /v2/payments/refunds/{id}` |
| Verify webhook | `POST /v1/notifications/verify-webhook-signature` |

Facts the code must respect:
- An **empty refund body means a full refund** → always send `amount`.
- Amount must be positive and in the capture's currency; partial refunds may add up to the captured total.
- Refunds are allowed only within **180 days** of capture (`REFUND_NOT_ALLOWED_AFTER_180_DAYS`).
- `PayPal-Request-Id` makes POSTs idempotent. Re-sending a used id returns the latest state of the original request: the refund endpoint answers **200** with the existing refund instead of **201**. **Confirmed live in the sandbox on Oct 7, 2026:** re-sending a refund's request id returned `200 OK` with the same refund id, and no second refund was made. How long PayPal keeps refund request ids is not documented **[VERIFY]**; minutes later still worked.
- Field limits (OpenAPI spec): `note_to_payer` ≤ 255 characters, `custom_id` ≤ 127 characters, `currency_code` exactly 3 characters.
- Zero-decimal currencies: HUF, JPY and TWD (no decimal places). Every other supported currency has 2.
- Refund `status`: `COMPLETED | PENDING | FAILED | CANCELLED`. P5 must decide what a 2xx with a non-`COMPLETED` status means for the ledger.
- Error body: `{name, message, debug_id, details:[{issue, description, field}]}`. Refund issues include `REFUND_AMOUNT_EXCEEDED`, `CAPTURE_FULLY_REFUNDED` and `REFUND_NOT_ALLOWED_AFTER_180_DAYS`.
- PayPal documents 409, 429, 5xx and network timeouts as safe to retry with the same `PayPal-Request-Id`.

**Seeding (`scripts/seed_sandbox.py`, logic in `wardlock/seed.py`):**
- **Input:** `seed/customers.json` and `seed/orders.json`. Amounts are strings, read as `Decimal`. Totals are always computed, never typed in. Emails must be `@example.com`.
- **Create:** create each order with `intent: CAPTURE`. `purchase_units[0]` carries:
  - `reference_id`;
  - `items[]`, where `quantity` is a **string**;
  - `amount.breakdown` with `item_total`, plus `shipping` when it is above 0;
  - `payment_source.paypal.experience_context` with `NO_SHIPPING`, `PAY_NOW`, `return_url` and `cancel_url`.

  This was checked against PayPal's OpenAPI spec. The approval link's `rel` is `payer-action`.
- **Payment, default (card mode):** each order is paid with one of PayPal's **published sandbox test cards** (`payment_source.card`). In this "single-step" create, the create call itself takes the payment and returns `COMPLETED` with the capture, so there's no browser and no approval.
  - `PayPal-Request-Id` is mandatory here, and PayPal keeps it for 6 hours (OpenAPI spec), so a retried or re-run create can't charge twice.
  - The card is only ever sent when the client points at the sandbox.
  - A card that needs 3D Secure (`PAYER_ACTION_REQUIRED`) is reported, and the next run creates a fresh order, e.g. with another card via `--card-number` from the dashboard's card generator.
- **Payment, fallback (`--approve-in-browser`):** print approval links, and you approve each one as a sandbox *personal* account.
- **Capture checks, both modes:** the script reads each order's `status` before capturing. `COMPLETED` was confirmed live on Oct 7, 2026 (13 card-paid orders). The other status values are still **[VERIFY]**. `APPROVED` → capture; `COMPLETED` → read the existing capture. A capture is recorded only if its status is `COMPLETED` **and** its amount and currency equal the seed total. A 202 or `PENDING` is retried on the next run.
- **`--only REF`** seeds a subset (e.g. `--only Z-9001` as a $10 first check).
- **The sandbox accounts must be US accounts.** PayPal creates sandbox accounts in the developer's own country by default. Indian accounts can't receive these USD payments ("This seller doesn't accept payments in your currency"). Use a US Business account and the REST app linked to it.
- **Resumable:** progress is saved to `seed/sandbox_state.json` (gitignored) after every step.
  - Create request IDs are `seed-{ref}-{contenthash}-{attempt}`, and capture request IDs are `seed-capture-{order id}`.
  - Re-running never duplicates an order or a row.
  - `--restore-db` rebuilds rows from the state file with no PayPal calls. It's the recovery path after a schema change, since there are no migrations.
  - Editing an order after it was captured is refused.
- **Smoke-test refunds:** use order `Z-9001` only. Refunds made outside Wardlock are not in the ledger, so they would distort `FULL_REMAINING` for demo orders.

## 8. Webhooks and reconciliation

- `POST /webhooks/paypal`: keep the **raw body**; verify with `verify-webhook-signature` using `PAYPAL_WEBHOOK_ID`; store in `webhook_events` (unique `event_id`); ignore duplicates.
- `PAYMENT.CAPTURE.REFUNDED`: read the refund `id`, `custom_id`, amount and capture id from the resource **[VERIFY resource shape, including that `custom_id` is echoed back and where the capture id lives]**. PayPal sends this event for Wardlock's *own* refunds too, so reconcile in this order:
  1. A `ledger` row has that `paypal_refund_id` → already recorded, nothing to do.
  2. `custom_id` names a `decisions` row **and** that decision's capture and amount match the event **and** the decision is not already linked to a different refund id → this is Wardlock's own refund whose response was lost. This happens when PayPal processed the call but Wardlock saw timeouts (§6 step 4), or when the webhook arrived before the ledger write. Write the missing `ledger` row and mark the decision's handles CONSUMED. If the decision has an open hold, close it as executed: denying that hold would release the handles and let the agent request the same refund again under a new request id. No alert.
  3. Otherwise → alert `OUT_OF_BAND_REFUND`. Checking capture and amount stops someone outside Wardlock from hiding a refund by copying a real decision id into `custom_id`.
- `ledger.paypal_refund_id` is unique. The executor and the webhook handler may race to write the same row. Whichever writes second does nothing.
- `PAYMENT.CAPTURE.REVERSED` → alert `CAPTURE_REVERSED`.
- Invalid signature → HTTP 400, store as unverified, no processing.
- Dev testing: PayPal Webhook Simulator + a tunnel to localhost.

## 9. Data model

Implemented in `wardlock/models.py`. Money is integer minor units. Timestamps are timezone-aware UTC. IDs are prefixed random strings (`ord_`, `ses_`, `dec_`, `hold_`, `rf_`, `alr_`; handles `h_`). Enum columns have database CHECK constraints, and SQLite runs with `PRAGMA foreign_keys=ON`.

| Table | Columns |
|---|---|
| `customers` | `id`, `first_name`, `email` (unique) |
| `orders` | `id`, `customer_id`, `order_ref` (unique), `status` (`PROCESSING \| SHIPPED \| DELIVERED`), `placed_at` (display only), `paypal_order_id` (unique), `capture_id` (unique), `currency`, `captured_minor`, `items_json` (`[{item_ref, name, unit_minor, quantity}]`), `shipping_minor`, `captured_at` |
| `sessions` | `id`, `ticket_id`, `customer_id`, `ticket_body` (untrusted), `token_hash` (unique), `created_at`, `expires_at`, `status` |
| `handles` | §4 columns plus `label` (shown to the agent) and `goodwill_pct` (GOODWILL_PCT only) |
| `decisions` | `id`, `session_id`, `tool`, `request_json`, `verdict`, `reason_codes`, `capture_id` (for §8 reconciliation), `amount_minor`, `currency`, `idempotency_key` (unique; NULL for BLOCKs), `policy_version`, `latency_ms`, `created_at` |
| `holds` | `id`, `decision_id` (unique), `frozen_request_json`, `status` (`PENDING \| APPROVED \| DENIED \| EXPIRED \| EXECUTED`), `approver`, `created_at` (for `HOLD_TIMEOUT_MINUTES`), `decided_at` |
| `ledger` | `id`, `decision_id` (unique), `capture_id`, `paypal_refund_id` (unique), `amount_minor`, `currency`, `status`, `created_at` |
| `webhook_events` | `event_id` (primary key), `event_type`, `resource_id`, `verified`, `payload_json`, `received_at` |
| `alerts` | `id`, `kind`, `details_json`, `created_at`, `acknowledged` |

v1.3 additions to the original §9: `orders.status` and `placed_at` (returned by §3.1), `decisions.capture_id`, `holds.created_at`, and `handles.label` and `goodwill_pct`.

## 10. Sessions API (orchestrator → Wardlock)

`POST /sessions` with header `X-Service-Key`. Body: `{ticket_id, customer_id, ticket_body}`. `ticket_id` and `customer_id` come from ticket **metadata** only. `ticket_body` is stored as **untrusted** text. It is used only by the reason check (§11) and the console's ticket replay (§12), never for ownership or amounts.
Returns `{session_id, session_token, expires_at}`. Store only a SHA-256 hash of the token. TTL `SESSION_TTL_MINUTES`.

**Errors (fail closed):**
- `WARDLOCK_SERVICE_KEY` unset → **503** for everyone;
- missing or wrong key → 401, checked in constant time and before the body is validated;
- unknown customer → 404;
- malformed body, unknown fields, or `ticket_body` over 50,000 characters → 422.

The token is `secrets.token_urlsafe(32)` (256 bits) and is returned only once.

## 11. LLM reason check (escalation only)

Runs only when the engine would otherwise ALLOW. Input: `reason_code`, handle labels, and the ticket body wrapped in clear "untrusted customer text" delimiters. Output must be JSON `{"consistent": true|false, "rationale": "<= 25 words"}`. Timeout 8 s, invalid JSON, or any error → treat as not consistent → HOLD.

## 12. Admin console

Basic auth (`WARDLOCK_ADMIN_PASSWORD`). Pages: **Decisions** (AG Grid: time, ticket, customer, amount, verdict, reason codes, latency), **Holds** (Approve / Deny), **Alerts**, **Ledger**, **Ticket replay**. Backend: `/admin/api/*` JSON endpoints.

**Ticket replay** (the main screen of the demo video) has three columns for one session:
1. The ticket body from `sessions.ticket_body`, rendered as **plain text, never HTML**. That stops XSS in the console and keeps hidden injections such as HTML comments visible. Highlight them.
2. Each refund tool call for that session from `decisions` (`tool`, `request_json`).
3. Each call's verdict, amount and reason codes, explained in plain words.

It is built only from existing tables. No new logging is needed.

## 13. Acceptance tests (all must pass)

| ID | Scenario | Expected |
|---|---|---|
| T1 | Handle minted in another session | BLOCK `HANDLE_OTHER_SESSION` |
| T2 | Invented handle string | BLOCK `UNKNOWN_HANDLE` |
| T3 | Reuse a consumed handle | BLOCK `HANDLE_ALREADY_USED` |
| T4 | Order re-assigned to another customer after minting | BLOCK `NOT_OWNER` |
| T5 | Handles from two captures | BLOCK `INVALID_COMBINATION` |
| T6 | `GOODWILL` with a `LINE_ITEM` handle | HOLD `REASON_KIND_MISMATCH` |
| T7 | Amount above limit | HOLD `OVER_AUTO_LIMIT` |
| T8 | Third refund in one session | HOLD `SESSION_VELOCITY` |
| T9 | PayPal times out 4× | 3 retries with the same `PayPal-Request-Id`, then HOLD |
| T10 | Any refund request body | always contains `amount` |
| T11 | Compat: other customer's capture | BLOCK `NOT_OWNER` |
| T12 | Compat: no amount | BLOCK `MISSING_AMOUNT` |
| T13 | Compat: amount not derivable | HOLD `AMOUNT_NOT_DERIVABLE` |
| T14 | Webhook refund id not in ledger | alert `OUT_OF_BAND_REFUND` |
| T15 | Webhook with bad signature | 400, not processed |
| T16 | Reason check times out | HOLD `REASON_NOT_SUPPORTED_BY_TICKET` |
| T17 | Approving a hold | executes the frozen request with the same idempotency key |
| T18 | 3 × 19.99 | exactly 59.97 (no float error) |
| T19 | Scripted malicious agent (forged handles, other sessions' handles, raw IDs) | zero money moved |
| T20 | Webhook for a Wardlock refund with no ledger row yet (`custom_id` = decision id; capture and amount match; decision has an open `DEPENDENCY_UNAVAILABLE` hold) | no alert; ledger row backfilled; handles CONSUMED; hold closed as executed |
| T21 | Webhook whose `custom_id` names a real decision but whose amount or capture differs | alert `OUT_OF_BAND_REFUND` |

## 14. Evaluation harness

Corpus: `eval/corpus/{attack,benign,edge}/*.json` using the ticket format in §2 plus `"expected": "ALLOW|HOLD|BLOCK|NO_ACTION"` and `"category"`.

Configurations:
- **A — Raw:** same LLM agent, PayPal's own agent toolkit (`create_refund`) directly.
- **B — Blanket approval:** Wardlock with every refund forced to HOLD.
- **C — Wardlock:** full engine.

Metrics: attack success rate (money moved on an attack ticket), benign auto-resolution rate, hold rate, false-block rate, p50/p95 latency added. Write results to `eval/RESULTS.md` with the exact model name and date. **Never edit numbers by hand.**
