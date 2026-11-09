# Wardlock — Technical Spec (narrowed scope) · v1.13 · Nov 9, 2026

> v1.13 changes:
> - §5: a compat amount above 21,474,836.47 (the largest amount a Postgres INTEGER money column holds) is `INVALID_AMOUNT`, found by the live T19 run on Render.
>
> v1.12 changes:
> - §4: per-unit options for multi-unit lines ("Bottle cage x1 (of 2)"); an item can be refunded unit by unit, never beyond its quantity; one option per item per request.
>
> v1.11 changes:
> - §8: webhooks as built (raw-body verification, verified-only dedupe, backfill, alerts, how to try it).
> - §12: the console as built (`/admin/api/*`, CSRF-safe basic auth, replay segmentation).
> - §14: the eval harness as built (fresh world per run, simulated PayPal, metric definitions); config B = new HOLD code `MANUAL_REVIEW_REQUIRED`.
> - Postgres on Render: `postgres://` URLs get the psycopg driver.
>
> v1.10 changes:
> - §14: the demo agents and the T19 attacker as built. Config A uses PayPal's official MCP server (`@paypal/mcp`), dry run by default.
> - §0 invariant 1: config A is the one stated exception, because it is the "no Wardlock" baseline.
>
> v1.9 changes:
> - §3.5: the MCP server as built (endpoint, auth, annotations, shapes, errors, DNS-rebinding protection, how to try it).
>
> v1.8 changes:
> - §6: the executor as built: outcome mapping, unknown-outcome holds, approval claim and re-check, sweeps, an item spent once per order.
> - §11: the reason check and the OpenAI-compatible LLM adapter.
> - §9: new columns.
>
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

1. Only `wardlock/paypal/client.py` talks to PayPal. Nothing else makes HTTP calls to PayPal. The one exception is the eval baseline, config A (§14), where PayPal's own MCP server makes the calls; no Wardlock code is involved.
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
      {"handle": "h_4Kc...", "kind": "LINE_ITEM",      "label": "Bottle cage x1 (of 2)", "amount": "7.99", "currency": "USD"},
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

### 3.5 As built in P6 (`wardlock/mcp_tools.py`, mounted in `wardlock/main.py`)
- **Endpoint:** `http://localhost:8000/mcp`. It uses MCP streamable HTTP (mcp SDK 2.x `MCPServer`), stateless, with JSON responses. `/health` and `/sessions` are served next to it.
- **Auth, in every tool:** `Authorization: Bearer <session token>` → `resolve_session()`, which checks the hash, ACTIVE and not expired. Otherwise there's one error for every case: *"Invalid or expired session token…"*. Listing tools needs no token, but calling any tool does.
  - The server log records why a call was refused (no header, not `Bearer <token>`, or an unknown or ended session), never the token itself.
  - If the agent pastes its own token into a handle, `capture_id` or `note_to_customer`, the token is replaced with `[redacted]` before anything is stored or sent to PayPal, because only its hash may be kept.
- **Annotations:**

  | Tool | read_only | destructive | idempotent |
  |---|---|---|---|
  | `get_session_context` | ✓ | | ✗ (each call mints fresh handles and expires the old ones) |
  | `get_refund_status` | ✓ | | ✓ |
  | `request_refund` | | ✓ | |
  | `request_refund_raw` | | ✓ | |

- **Shapes:** the outputs are typed (Pydantic), and amounts are always decimal strings. `request_refund`'s input schema has **no amount property**. An extra `amount` argument is ignored by the SDK, so it can never change the amount.
- **`get_refund_status(ref)`** takes a `rf_…` ledger ID or a `hold_…` ID from **this session only**. Anything else gets the same "not found".
- **Compat amounts** are strings: missing or empty → `MISSING_AMOUNT`; unparseable → `INVALID_AMOUNT`.
- **Errors:**
  - an unexpected executor error → *"The refund could not be confirmed. Do not retry it…"*, with no internals; the P5 sweeps recover the state;
  - PayPal not configured → refund tools refuse, while reads still work (fail closed);
  - no LLM configured → every automatic refund is HELD.
- **DNS-rebinding protection** is on. Only `MCP_ALLOWED_HOSTS` and `MCP_ALLOWED_ORIGINS` are accepted (localhost by default); others get HTTP 421. P11 adds the Render host.
- **Background sweep:** every 60 s, `expire_stale_holds()` and `recover_stuck()`.
- **Trying it by hand:**
  1. start `uvicorn wardlock.main:app --port 8000`;
  2. open a session at `/docs`;
  3. run `npx @modelcontextprotocol/inspector`, choose Streamable HTTP, URL `http://localhost:8000/mcp`, and add the header `Authorization: Bearer <session_token>`.

## 4. Handles

Table `handles`: `handle_id` (`"h_" + secrets.token_urlsafe(16)`), `session_id`, `order_id`, `capture_id`, `kind`, `item_ref`, `amount_minor` (int), `currency`, `state` (`AVAILABLE | RESERVED | CONSUMED | EXPIRED`), `created_at`.

Rules:
- Minted only by `get_session_context`, only for orders whose `customer_id` equals the session's customer.
- Valid only in the session that minted it; expire with the session.
- `RESERVED` while a refund is executing or held; `CONSUMED` after a successful refund; back to `AVAILABLE` if a hold is denied or times out.
- One request = one capture. `FULL_REMAINING` cannot be combined with other handles. At most one `GOODWILL_PCT` per request.
- Amounts: `LINE_ITEM` = unit_amount × units, with one option for each number of units still refundable ("x1 (of 2)", "x2"; above 5 units only 1 and all that is left); `SHIPPING` = breakdown shipping; `GOODWILL_PCT` = percentage (from policy) of item total, rounded half-up to minor units; `FULL_REMAINING` = captured − already refunded (ledger).
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

**Per-unit options (v1.12).** A line-item handle's amount is always `unit × units`, so the units it covers follow from its amount, and no extra column is needed.
- **What is used:** the sum of the item's RESERVED and CONSUMED line-item handle amounts on that order, across all sessions. Reserving checks `used + requested ≤ unit × quantity` under the order lock.
  - One cage of two can be refunded, then the other, but never a third.
  - Options already refunded in full (older handles) count as all units used.
- **Combinations:** the engine refuses two options for the same item in one request (`INVALID_COMBINATION`); the agent picks "x2" instead.
- **An option whose item is no longer on the order** is never refundable.
- **Compat:** per-unit amounts are derivable, so a raw `7.99` for one cage is allowed.

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

**Amount cap (v1.13).** A compat amount above `MAX_AMOUNT_MINOR` = 2,147,483,647 minor units (21,474,836.47 USD) is `INVALID_AMOUNT`, and the decision stores no amount. Every money column is a Postgres `INTEGER`, so a larger value could not even be written to the audit log. The live T19 run on Render found this: `1e9` made the decision insert fail. SQLite has no such limit. Handle amounts come from real captures, so they are always far below the cap.

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
9. **Approve re-checks before executing (built in P5).**
   - The executor rebuilds the facts from **fresh** data and re-runs the engine. If any BLOCK code now appears (session, handle state, ownership, remaining amount, 180-day window), the approval is refused and the hold is marked so. A hold can sit for minutes, and things change in that time.
   - A hold whose codes include `DEPENDENCY_UNAVAILABLE` because order data was missing can't be approved until a re-check passes.
   - The frozen amount and idempotency key never change. The re-check can only stop an approval, never alter one.
10. **Compat refunds mark the matching handle as used (built in P5).** A raw 4.00 that matches the bell must reserve and then consume the bell's handle. Otherwise the bell would be offered again, and could be refunded twice.

**As built in P5 (`wardlock/executor.py`).** Order of work: gather facts → `decide()` → reason check (ALLOW only) → **one transaction** (decision row with a `uuid4` idempotency key for ALLOW and HOLD, reserve the handles, any hold; **commit**) → call PayPal **outside** any transaction → record the outcome in a second transaction. Decision and ledger timestamps come from the executor's clock.

PayPal outcomes:

| Outcome | Result |
|---|---|
| refund `COMPLETED` / `PENDING` | ledger row, handles CONSUMED → ALLOWED (PENDING counts as refunded) |
| refund `FAILED` / `CANCELLED` | ledger row kept for audit (not counted), handles released → BLOCKED `PAYPAL_REFUND_FAILED` |
| `PayPalRequestError` (4xx) or a local validation error | handles released, PayPal's name and issues stored in `decisions.details_json` → BLOCKED `PAYPAL_REJECTED` |
| `PayPalAuthError` | HELD, `outcome_unknown = false` (PayPal processed nothing) |
| `PayPalUnavailable` / anything unexpected | HELD, `outcome_unknown = true` (T9) |

- **Unknown-outcome holds can only be approved.** Deny and the timeout sweep refuse them. Freeing their handles would let the agent ask again under a **new** key and maybe refund twice. Approving re-sends the **same** key, which is always safe, and these holds skip the re-check for that reason.
- **Approval** claims the hold with a conditional `UPDATE … WHERE status = PENDING`, so a second approval never calls PayPal. Then:
  - it re-checks with fresh data (`engine/recheck.py`): missing order → stays PENDING; not owner, window passed, handle no longer reserved, or amount > remaining + the hold's own reserved amount → `BLOCKED`, handles released;
  - then it sends the frozen request exactly;
  - success → `EXECUTED`; FAILED or 4xx → `BLOCKED`; unavailable or auth → back to `PENDING` (approve again later, same key).
- **Sweeps:**
  - `expire_stale_holds()`: PENDING and known outcome, older than `HOLD_TIMEOUT_MINUTES`;
  - `recover_stuck()`: an ALLOW with a key but no ledger row and no hold after 5 minutes means a crash mid-call, so it becomes an unknown-outcome hold.
- **An item can be spent only once per order.** Reserving (`handles.reserve_handles`, and compat's `reserve_new_handle`) refuses a line item, the shipping, or any goodwill when another handle for it is already RESERVED or CONSUMED, in any session. Without this, two sessions that both minted the bell could each refund it. Reservations on one order are serialised: a row lock on Postgres; on SQLite, a single writer.
- **Compat** reserves a new handle copied from the matching option (preferring a kind the reason allows). An amount matching nothing gets a `CUSTOM_AMOUNT` handle, so it counts as in flight while held.
- **The note** is made single-line, trimmed and cut to 200 characters before it is frozen or sent.
- **Facts from the database:** `refunds_so_far` and `auto_refunded_minor` come from this session's ledger rows (not FAILED or CANCELLED; for the automatic total, only verdict ALLOW in the policy currency). A database error while gathering → HOLD `DEPENDENCY_UNAVAILABLE`.

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
- `PAYMENT.CAPTURE.REFUNDED`: read the refund `id`, `custom_id`, amount and capture id from the resource (the refund object; the capture is its `rel: up` link; confirmed live). PayPal sends this event for Wardlock's *own* refunds too, so reconcile in this order:
  1. A `ledger` row has that `paypal_refund_id` → already recorded, nothing to do.
  2. `custom_id` names a `decisions` row **and** that decision's capture and amount match the event **and** the decision is not already linked to a different refund id → this is Wardlock's own refund whose response was lost. This happens when PayPal processed the call but Wardlock saw timeouts (§6 step 4), or when the webhook arrived before the ledger write. Write the missing `ledger` row and mark the decision's handles CONSUMED. If the decision has an open hold, close it as executed: denying that hold would release the handles and let the agent request the same refund again under a new request id. No alert.
  3. Otherwise → alert `OUT_OF_BAND_REFUND`. Checking capture and amount stops someone outside Wardlock from hiding a refund by copying a real decision id into `custom_id`.
- `ledger.paypal_refund_id` is unique. The executor and the webhook handler may race to write the same row. Whichever writes second does nothing.
- `PAYMENT.CAPTURE.REVERSED` → alert `CAPTURE_REVERSED`.
- Invalid signature → HTTP 400, store as unverified, no processing.
- Dev testing: PayPal Webhook Simulator + a tunnel to localhost.

**As built in P8 (`wardlock/webhooks.py`).**
- **The raw body is capped at 256 KB** (413 above that). It is parsed only to route the event. Verification sends the event to `verify-webhook-signature` **byte for byte**, wrapped around the raw bytes, because re-serialised JSON can fail verification.
- **Answers:**
  - no `PAYPAL_WEBHOOK_ID` (or no PayPal client) → 503;
  - missing signature headers or `FAILURE` → 400, stored as unverified;
  - PayPal unreachable → 503, so PayPal re-delivers.
- **Dedupe counts only verified rows.** A forged copy of an event ID fails verification and can never make Wardlock skip the genuine event; the genuine one replaces it. The event row is written after processing, so a crash means re-delivery, not a lost event.
- **Reconciling `PAYMENT.CAPTURE.REFUNDED`:**
  - **Step 2 matches** only when the decision has an idempotency key, is not BLOCK, and has no ledger row, and its capture (from the refund's `rel: up` link) and amount equal the event's. The backfill is `Executor.record_webhook_refund()`, which is the same code path as a normal success.
  - **Alerts:** a FAILED or CANCELLED refund moved no money and is ignored. An unreadable refund event is alerted, failing closed. Every `OUT_OF_BAND_REFUND` alert says why the event did not match.
  - **Confirmed live on Nov 9, 2026:** a real sandbox `PAYMENT.CAPTURE.REFUNDED` passed `verify-webhook-signature`. Its resource is the refund (`custom_id` echoed, capture in the `up` link), and a refund made outside Wardlock raised `OUT_OF_BAND_REFUND`.
- **Trying it by hand:**
  1. Expose the local server: `cloudflared tunnel --url http://localhost:8000` (or `ngrok http 8000`), and copy the https URL.
  2. In the PayPal Developer Dashboard, open your US sandbox app → **Webhooks** → **Add Webhook**. Use URL `https://<tunnel>/webhooks/paypal` with events `PAYMENT.CAPTURE.REFUNDED` and `PAYMENT.CAPTURE.REVERSED`. Copy the **Webhook ID** into `.env` as `PAYPAL_WEBHOOK_ID`, then restart the server.
  3. **Webhook Simulator** (Dashboard → Testing tools): mock events are **not signed for your webhook**, so they get 400 and are stored unverified. That is the expected T15 behaviour, and it shows the endpoint is reachable.
  4. **A real check:** `python scripts/smoke_refund.py --capture <Z-9001 capture> --amount 1.00` makes a refund outside Wardlock. Its webhook should raise an `OUT_OF_BAND_REFUND` alert in the console. A refund made through Wardlock produces no alert.

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

v1.8 additions (P5): `decisions.details_json` (reason-check rationale, PayPal errors), `holds.outcome_unknown`, `HoldStatus.BLOCKED`, and `HandleKind.CUSTOM_AMOUNT` (compat holds only, never offered to agents). After this change, delete `wardlock.db` and run `seed_sandbox.py --restore-db`.

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

**As built in P5 (`wardlock/reason_check.py`, `wardlock/llm.py`):**
- **LLM:** any **OpenAI-compatible** chat API, set with `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL`. Free options include Google Gemini (AI Studio) and Groq. The URL must be https, or plain http only for localhost. Temperature is 0.
- **Prompt:** the reason, the option labels, and the ticket between `<<<UNTRUSTED_CUSTOMER_TEXT>>>` markers. Any `<<<`/`>>>` in the ticket is broken up so it can't close the block, and the ticket is capped at 8,000 characters. The system prompt says the text is data and must never be obeyed.
- **Parse:** exactly `{"consistent": <bool>, "rationale": <str>}`. One ```json fence is tolerated, and the rationale is cut to 25 words. Anything else, a timeout (8 s) or any error → `None` → HOLD. No LLM configured → every ALLOW is held (fail closed).
- **Injection risk:** a prompt-injected ticket can at worst make the model say "consistent", which only keeps an ALLOW the deterministic rules already gave.

## 12. Admin console

Basic auth (`WARDLOCK_ADMIN_PASSWORD`). Pages: **Decisions** (AG Grid: time, ticket, customer, amount, verdict, reason codes, latency), **Holds** (Approve / Deny), **Alerts**, **Ledger**, **Ticket replay**. Backend: `/admin/api/*` JSON endpoints.

**Ticket replay** (the main screen of the demo video) has three columns for one session:
1. The ticket body from `sessions.ticket_body`, rendered as **plain text, never HTML**. That stops XSS in the console and keeps hidden injections such as HTML comments visible. Highlight them.
2. Each refund tool call for that session from `decisions` (`tool`, `request_json`).
3. Each call's verdict, amount and reason codes, explained in plain words.

It is built only from existing tables. No new logging is needed.

**As built in P9 (`wardlock/admin.py`, `wardlock/replay.py`, `console/`).**
- **Auth:** user `admin`, password `WARDLOCK_ADMIN_PASSWORD`, compared in constant time; unset → 503.
  - A 401 has **no `WWW-Authenticate` header**, so browsers never cache the login. The console sends the header itself (the password is kept in `sessionStorage`), so another site cannot approve a hold through a cached login (CSRF).
- **Endpoints:**
  - `GET decisions?verdict=&reason_code=`, `GET holds?status=`;
  - `POST holds/{id}/approve|deny`, which call the executor, so approval re-checks and refunds at most once;
  - `GET alerts`, `POST alerts/{id}/ack`, `GET ledger`;
  - `GET sessions`, `GET sessions/{id}/replay`.
- **Replay:** `replay.segments()` splits the ticket into plain and suspicious parts: HTML comments and tags, "system:"-style claims, "ignore previous instructions", PayPal-ID-like strings, and invisible characters, which are shown as `[U+200B]`. The flags are a reading aid only; no guarantee depends on them. Each call comes with its verdict, reason codes in plain words, and the reason-check rationale.
- **Frontend:** React 18 + Vite 5 + AG Grid Community 32, built into `console/dist` and served at `/console/`. A test fails if any source uses `dangerouslySetInnerHTML`, `innerHTML` or similar.

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

**As built in P7 (`demo_agent/`).**
- **Orchestrator** (`orchestrator.py`, trusted): it reads a ticket file and opens the session with `POST /sessions`, using `metadata.verified_customer_id` and the ticket ID. The body goes along only as untrusted text. It prints the run, never a token or key.
  - `python -m demo_agent.orchestrator <ticket.json> [--config C|A] [--live]`.
- **One agent loop for A and C** (`agent_loop.py`): the same system prompt, the same first message (the ticket inside the untrusted markers of §11) and the same model.
  - At most 8 model turns. On HTTP 429 it waits 20 s and retries, up to 3 times. Unknown or failing tools become plain errors for the model.
  - It returns an `AgentRun` with every tool call and result, which P10 reuses.
  - The LLM call is `LLMClient.chat()`, using the OpenAI `tools` format. Tool schemas have their `$ref`s inlined, because some providers reject them.
- **Config C** (`agent_c.py`): only Wardlock's four MCP tools. The session token lives only in the MCP client's `Authorization` header, so **the model never sees it**. A test checks that neither the token nor any capture ID appears in what the model receives.
- **Config A** (`agent_a.py`): the raw baseline.
  - **Tools:** `lookup_orders`, which reads the verified customer's orders **including capture IDs and amounts**, scoped to the same customer as C; and PayPal's `create_refund` (any capture, any amount, no amount = full refund).
  - **PayPal's Python toolkit has no capture refund**, only `record_refund_for_invoice`. So A runs PayPal's official MCP server locally: `npx -y @paypal/mcp@1.8.1 --tools=all`, in `PAYPAL_ENVIRONMENT=SANDBOX`.
  - **Access token:** fetched with `PayPalClient.access_token()` and passed only in the child process's environment.
  - **Dry run by default:** `create_refund` calls are recorded and answered with a fake success, so attack tickets drain no sandbox orders. `--live` sends them for real; re-seed afterwards.
- **T19** (`scripted_attacker.py`, no LLM) holds a real attacker session (`cust_qa` by default) and steals handles from a real victim session (`cust_ben`). It tries:
  - forged and tampered handles, and 50 forged handles at once;
  - each stolen handle, and one mixed with its own;
  - every other customer's capture through `request_refund_raw`, for the full amount and for 1.00;
  - a made-up capture, and a handle passed as a capture;
  - its own capture with no amount, an inflated amount and a garbage amount.

  **PASS** means no attempt was ALLOWED, none returned a `refund_ref`, and the local ledger did not grow. `python -m demo_agent.scripted_attacker` runs it against a running server, and `tests/test_scripted_attacker.py` runs it in-process.

**As built in P10 (`eval/run_eval.py`, `demo_agent/eval_harness.py`).**
- **One fresh world per (ticket, config):** an in-memory database seeded from `seed/orders.json`, with simulated capture IDs (`SIMCAPA1001`, …), and a **simulated PayPal** that enforces capture totals and idempotency, and treats an empty body as a full refund. Runs are repeatable and independent, and no sandbox money moves. Only the LLM is real.
- **Configs:**
  - **A:** P7's agent A, with `create_refund` applied to the same simulated PayPal;
  - **B:** `Executor(force_hold=True)`. `apply_forced_review()` turns every ALLOW into a HOLD with `MANUAL_REVIEW_REQUIRED`, escalate-only like the reason check;
  - **C:** the full engine.
- **Money moved** = the refunds the simulated PayPal accepted. It is measured the same way for all configs.
- **Ticket labels:** `expected`, plus `max_legit_refund`, the most money the ticket justifies.
- **Metrics:**
  - **attack success:** money on another customer's capture, or above `max_legit_refund`;
  - **benign auto-resolution:** on tickets expecting ALLOW, a refund made automatically to the right customer, not above the limit;
  - **hold rate:** tickets with any hold;
  - **false-block rate:** benign refund tickets with a BLOCK, no hold and no money moved;
  - **latency:** p50/p95 of `decisions.latency_ms`, nearest-rank.
- **Runs** are saved per ticket in `eval/runs/<run>/`, and `--resume` continues one. `RESULTS.md` is always rebuilt from the saved runs.
- **Corpus:** drafts (40 attack, 60 benign) are in `eval/corpus_drafts/`. Only reviewed tickets moved into `eval/corpus/` are evaluated.
