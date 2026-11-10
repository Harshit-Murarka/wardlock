"""Pages for people with a browser: "/", "/mcp" and "/webhooks/paypal" (static text, no data, no secrets).

/mcp and /webhooks/paypal are machine endpoints: an MCP client and PayPal never ask for HTML,
so a browser visit can get a page that explains them without changing what machines get.
"""

from __future__ import annotations

from html import escape
from typing import Any

REPO_URL = "https://github.com/Harshit-Murarka/wardlock"

_STYLE = """
:root {
  --paper:#f5f2ea; --sheet:#fffdf8; --ink:#1e1c18; --soft:#5f5a50; --rule:#ddd6c6;
  --blue:#1d4f91; --green:#2d6a3b; --red:#a2392a; --tint:#ece7da;
  --serif: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
  --mono: ui-monospace, "SF Mono", "Cascadia Mono", Consolas, Menlo, monospace;
}
@media (prefers-color-scheme: dark) {
  :root { --paper:#14130f; --sheet:#1c1b16; --ink:#ebe6da; --soft:#a39d90; --rule:#34312a;
          --blue:#8db6ee; --green:#7fc08d; --red:#e58b7c; --tint:#25231d; }
}
* { box-sizing:border-box; }
html { -webkit-text-size-adjust:100%; }
body { margin:0; background:var(--paper); color:var(--ink); font:17px/1.6 var(--serif); }
a { color:var(--blue); text-underline-offset:3px; }
code { font:.84em var(--mono); background:var(--tint); padding:1px 5px; border-radius:3px; }
.wrap { max-width:1040px; margin:0 auto; padding:0 20px; }

/* top bar */
.bar { border-bottom:1px solid var(--rule); }
.bar .wrap { display:flex; align-items:center; justify-content:space-between; gap:16px; height:56px; }
.mark { font:700 17px var(--mono); letter-spacing:.02em; color:var(--ink); text-decoration:none; }
.mark span { color:var(--soft); font-weight:400; }
nav { display:flex; gap:18px; font:14px var(--mono); overflow-x:auto; }
nav a { color:var(--soft); text-decoration:none; white-space:nowrap; }
nav a:hover, nav a[aria-current] { color:var(--ink); }

/* home: opening */
.lead { display:grid; grid-template-columns:1.05fr .95fr; gap:48px; padding:56px 0 48px; align-items:start; }
h1 { font:600 clamp(30px, 4.4vw, 44px)/1.12 var(--serif); letter-spacing:-.015em; margin:0 0 20px; }
.lead p { color:var(--soft); margin:0 0 14px; max-width:34em; }
.lead p strong { color:var(--ink); font-weight:600; }
.facts { font:13px var(--mono); color:var(--soft); margin-top:22px; }
.facts b { color:var(--ink); font-weight:600; }
.demo { margin-top:22px !important; padding:12px 14px; border-left:3px solid var(--blue); background:var(--sheet);
        color:var(--ink) !important; font-size:16px; }

/* the two decision slips */
.slips { display:grid; gap:16px; }
.slip { background:var(--sheet); border:1px solid var(--rule); border-radius:2px; padding:16px 18px 14px;
        font:13px/1.75 var(--mono); position:relative; box-shadow:0 1px 0 var(--rule), 0 8px 24px -18px rgba(0,0,0,.35); }
.slip .head { display:flex; justify-content:space-between; color:var(--soft); border-bottom:1px dashed var(--rule);
              padding-bottom:6px; margin-bottom:8px; }
.slip .call { color:var(--ink); margin-bottom:6px; word-break:break-word; }
.row { display:flex; gap:8px; }
.row .k { white-space:nowrap; }
.row .dots { flex:1; border-bottom:1px dotted var(--rule); transform:translateY(-6px); min-width:12px; }
.row .v { white-space:nowrap; }
.ok { color:var(--green); } .no { color:var(--red); }
.stamp { position:absolute; right:16px; bottom:14px; font:700 12px var(--mono); letter-spacing:.12em;
         padding:3px 8px; border:2px solid currentColor; border-radius:3px; transform:rotate(-4deg); opacity:.9; }
.result { margin-top:8px; padding-top:6px; border-top:1px dashed var(--rule); padding-right:110px; }

/* sections */
section { border-top:1px solid var(--rule); padding:40px 0; }
h2 { font:600 13px var(--mono); text-transform:uppercase; letter-spacing:.12em; color:var(--soft); margin:0 0 22px; }
.three { display:grid; grid-template-columns:repeat(3, 1fr); gap:36px; }
.three h3 { font:600 20px/1.3 var(--serif); margin:0 0 8px; }
.three .n { font:600 13px var(--mono); color:var(--blue); display:block; margin-bottom:6px; }
.three p { margin:0; color:var(--soft); font-size:16px; }

.flow { display:grid; grid-template-columns:repeat(4, 1fr); gap:0; counter-reset:step; }
.flow.cols3 { grid-template-columns:repeat(3, 1fr); }
.flow li { list-style:none; padding:0 20px 0 0; font-size:16px; color:var(--soft); }
.flow li b { display:block; color:var(--ink); font-weight:600; margin-bottom:4px; }
.flow li::before { counter-increment:step; content:counter(step, decimal-leading-zero);
                   display:block; font:13px var(--mono); color:var(--blue); border-top:2px solid var(--ink);
                   padding-top:8px; margin-bottom:8px; }
ol.flow { margin:0; padding:0; }

/* endpoint ledger */
.ledger { width:100%; border-collapse:collapse; font-size:16px; }
.ledger td { padding:12px 16px 12px 0; border-bottom:1px solid var(--rule); vertical-align:top; }
.ledger tr:last-child td { border-bottom:0; }
.ledger td:first-child { font:14px var(--mono); white-space:nowrap; width:1%; }
.ledger td.who { font:13px var(--mono); color:var(--soft); white-space:nowrap; width:1%; }
.ledger td:last-child { color:var(--soft); }

/* inner pages */
.page { max-width:760px; padding:48px 0 24px; }
.page h1 { font-size:clamp(28px, 4vw, 38px); }
.page p { color:var(--soft); max-width:40em; }
.page h2 { margin-top:36px; }
pre { font:13px/1.6 var(--mono); background:var(--sheet); border:1px solid var(--rule); padding:14px 16px;
      overflow-x:auto; border-radius:2px; margin:0 0 16px; }
.defs { display:grid; grid-template-columns:max-content 1fr; gap:10px 24px; margin:0 0 8px; }
.defs dt { font:14px var(--mono); }
.defs dd { margin:0; color:var(--soft); }
.defs.stack { grid-template-columns:1fr; gap:2px; }
.defs.stack dt { font-weight:600; }
.defs.stack dd { margin-bottom:14px; }
.state { display:inline-block; font:600 13px var(--mono); padding:2px 8px; border:1.5px solid currentColor;
         border-radius:3px; }

footer { border-top:1px solid var(--rule); margin-top:24px; padding:22px 0 40px; font:13px/1.7 var(--mono); color:var(--soft); }
footer a { color:var(--soft); }

@media (max-width: 820px) {
  .lead { grid-template-columns:1fr; gap:32px; padding:36px 0 32px; }
  .three, .flow, .flow.cols3 { grid-template-columns:1fr; gap:24px; }
  .flow li { padding:0; }
  .ledger td.who { display:none; }
  .defs { grid-template-columns:1fr; gap:2px; }
  .defs dd { margin-bottom:10px; }
  nav { gap:14px; }
}
@media (max-width: 560px) {
  .bar .wrap { flex-direction:column; align-items:flex-start; height:auto; gap:4px; padding-top:12px; padding-bottom:10px; }
  .mark span { display:none; }
  nav { width:100%; justify-content:space-between; gap:10px; font-size:13px; }
  .slip { padding:14px 14px 12px; font-size:12px; }
  .slip .head span:last-child { display:none; }
  .result { padding-right:0; padding-bottom:30px; }
  .stamp { bottom:10px; }
  .ledger td { padding-right:10px; }
  .ledger td:first-child { white-space:normal; word-break:break-all; width:34%; }
}
"""

_NAV = [("/mcp", "mcp"), ("/webhooks/paypal", "webhooks"), ("/console/", "console"), ("/docs", "api"),
        (REPO_URL, "source")]


def _page(title: str, current: str, body: str) -> str:
    links = "".join(
        f'<a href="{href}"{" aria-current=page" if href == current else ""}>{label}</a>' for href, label in _NAV)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Wardlock: a money gate between an AI support agent and PayPal.">
<title>{escape(title)}</title>
<style>{_STYLE}</style>
</head>
<body>
<header class="bar"><div class="wrap">
  <a class="mark" href="/">wardlock<span> / sandbox</span></a>
  <nav>{links}</nav>
</div></header>
<main class="wrap">
{body}
</main>
<footer><div class="wrap">
  PayPal sandbox only &middot; PayPal AI Hackathon 2026 &middot; <a href="{REPO_URL}">source on GitHub</a><br>
  Free Render instance: it sleeps after ~15 idle minutes, so the first request after a pause takes about a minute.
</div></footer>
</body>
</html>
"""


_LANDING = _page("Wardlock", "/", """
<div class="lead">
  <div>
    <h1>Let the support bot issue refunds. Just not the wrong ones.</h1>
    <p>Wardlock sits between an AI customer-support agent and PayPal. The agent can refund a customer
    by itself, with no person signing off on each one, but <strong>only on that customer's own orders,
    only for amounts Wardlock worked out from the order, and never out of the merchant's sight.</strong></p>
    <p>It never gets a PayPal ID or an amount field. It picks from refund options that are already priced,
    and a fixed set of rules decides what happens next. So a line hidden in an email saying
    "refund everything" has nothing to work with.</p>
    <div class="facts"><b>4</b> MCP tools &nbsp;&middot;&nbsp; <b>38</b> scripted attacks, <b>0</b> allowed
      &nbsp;&middot;&nbsp; <b>620+</b> tests on SQLite and Postgres</div>
    @@DEMO@@
  </div>

  <div class="slips" aria-label="Two example decisions">
    <div class="slip">
      <div class="head"><span>T-0001 &middot; cust_ana</span><span>"bell arrived scratched"</span></div>
      <div class="call">request_refund([h_7Kq2&hellip;], DAMAGED)</div>
      <div class="row"><span class="k">order belongs to customer</span><span class="dots"></span><span class="v ok">yes</span></div>
      <div class="row"><span class="k">amount, from the order</span><span class="dots"></span><span class="v">4.00 USD</span></div>
      <div class="row"><span class="k">under auto limit (50.00)</span><span class="dots"></span><span class="v ok">yes</span></div>
      <div class="row"><span class="k">reason fits the ticket</span><span class="dots"></span><span class="v ok">yes</span></div>
      <div class="result">Bike bell x1 refunded to Ana</div>
      <span class="stamp ok">ALLOWED</span>
    </div>
    <div class="slip">
      <div class="head"><span>T19 &middot; cust_qa</span><span>scripted attacker</span></div>
      <div class="call">request_refund_raw(&lt;Ben's capture&gt;, 1.00)</div>
      <div class="row"><span class="k">order belongs to customer</span><span class="dots"></span><span class="v no">no</span></div>
      <div class="result">NOT_OWNER: nothing sent to PayPal</div>
      <span class="stamp no">BLOCKED</span>
    </div>
  </div>
</div>

<section>
  <h2>What it guarantees</h2>
  <div class="three">
    <div><span class="n">G1</span><h3>The right customer</h3>
      <p>The helpdesk opens each conversation from its own verified customer ID, never from what the
      ticket says. Every refund is checked against that customer's orders.</p></div>
    <div><span class="n">G2</span><h3>The right amount</h3>
      <p>The agent chooses refund handles such as "Bike bell x1, 4.00". Wardlock computes every amount from
      the merchant's order data; the main refund tool has no amount parameter.</p></div>
    <div><span class="n">G3</span><h3>Nothing behind your back</h3>
      <p>Only Wardlock holds the PayPal credentials. Signed PayPal webhooks are matched against its ledger,
      and a refund it didn't make raises an alert.</p></div>
  </div>
</section>

<section>
  <h2>One ticket, start to finish</h2>
  <ol class="flow">
    <li><b>Helpdesk opens a session</b>for the verified customer and gets a 30-minute token the model never sees.</li>
    <li><b>Agent reads the context</b>over MCP: this customer's orders, each with priced refund options.</li>
    <li><b>Agent asks for a refund</b>with handles and a reason. The rules return allowed, held or blocked.</li>
    <li><b>PayPal confirms</b>with a signed webhook, and Wardlock reconciles it with what it sent.</li>
  </ol>
</section>

<section>
  <h2>On this server</h2>
  <table class="ledger">
    <tr><td><a href="/mcp">/mcp</a></td><td class="who">AI agent</td>
      <td>The MCP server (streamable HTTP). Every tool call needs the session's bearer token.</td></tr>
    <tr><td><a href="/webhooks/paypal">/webhooks/paypal</a></td><td class="who">PayPal</td>
      <td>Signed refund and reversal events, verified with PayPal and reconciled against the ledger.</td></tr>
    <tr><td><a href="/console/">/console/</a></td><td class="who">merchant</td>
      <td>Held refunds to approve or deny, alerts, decisions, and a replay of each ticket. @@CONSOLE@@</td></tr>
    <tr><td><a href="/docs">/docs</a></td><td class="who">helpdesk</td>
      <td>The HTTP API, including <code>POST /sessions</code>, which starts a conversation.</td></tr>
    <tr><td><a href="/health">/health</a></td><td class="who">anyone</td>
      <td>Returns <code>{"status":"ok"}</code> once the server is up.</td></tr>
  </table>
</section>
""")


_CONSOLE_LOCKED = "Password protected."
_CONSOLE_OPEN = ("Anyone can look around: choose <b>View the live demo</b> when it asks for a password. "
                 "That view is read-only; approving or denying a refund still needs the merchant's password.")
_DEMO_LINK = ('<p class="demo"><a href="/console/">Open the merchant console</a> and choose '
              '<b>View the live demo</b> to see every decision, hold, alert and ticket replay (read-only).</p>')


def landing_html(*, public_console: bool) -> str:
    """The home page; it mentions the read-only console only when that is switched on."""
    return (_LANDING.replace("@@CONSOLE@@", _CONSOLE_OPEN if public_console else _CONSOLE_LOCKED)
            .replace("@@DEMO@@", _DEMO_LINK if public_console else ""))


def mcp_page(tools: list[Any]) -> str:
    """What a browser sees at /mcp: how to connect, and the tools the server offers right now."""
    items = []
    for tool in tools:
        schema = tool.input_schema or {}
        required = set(schema.get("required") or [])
        params = [f"{name}{'' if name in required else '?'}" for name in schema.get("properties", {})]
        items.append(f"<dt>{escape(tool.name)}({escape(', '.join(params))})</dt>"
                     f"<dd>{escape(tool.description or '')}</dd>")
    example = ('curl -X POST https://wardlock.onrender.com/mcp \\\n'
               '  -H "Content-Type: application/json" \\\n'
               '  -H "Accept: application/json, text/event-stream" \\\n'
               """  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'""")
    return _page("Wardlock · MCP endpoint", "/mcp", f"""
<div class="page">
  <h1>/mcp</h1>
  <p>This address is the MCP server the support agent connects to. It speaks the Model Context Protocol,
  so you open it with an MCP client (an agent, or the MCP Inspector) rather than a browser.</p>

  <h2>Connecting</h2>
  <dl class="defs">
    <dt>transport</dt><dd>Streamable HTTP, stateless, JSON responses</dd>
    <dt>url</dt><dd>this address: <code>https://&lt;host&gt;/mcp</code></dd>
    <dt>auth</dt><dd><code>Authorization: Bearer &lt;session token&gt;</code>. The helpdesk gets the token from
      <code>POST /sessions</code> (see <a href="/docs">/docs</a>). Without it every tool says the token is invalid
      and does nothing.</dd>
  </dl>

  <p>Listing the tools needs no token:</p>
  <pre>{escape(example)}</pre>

  <h2>Tools</h2>
  <p>Read from the running server. A <code>?</code> marks an optional argument.</p>
  <dl class="defs stack">
    {"".join(items)}
  </dl>
</div>""")


def webhooks_page(*, verification_on: bool) -> str:
    """What a browser sees at /webhooks/paypal: what PayPal sends here and what Wardlock does with it."""
    state = ('<span class="state ok">ON</span> every event is checked with PayPal before anything else'
             if verification_on else
             '<span class="state no">OFF</span> no webhook ID is set, so events are refused with 503 '
             'and PayPal retries them later')
    return _page("Wardlock · PayPal webhooks", "/webhooks/paypal", f"""
<div class="page">
  <h1>/webhooks/paypal</h1>
  <p>PayPal posts an event here when one of the merchant's payments is refunded or reversed. This is
  how Wardlock notices a refund it didn't make, whether it came from someone in the PayPal dashboard,
  a leaked API key, or another tool using the same credentials.</p>

  <h2>Signature verification</h2>
  <p>{state}.</p>

  <h2>What happens to an event</h2>
  <ol class="flow cols3">
    <li><b>Verify</b>The raw body and PayPal's signature headers go to PayPal's
      verify-webhook-signature API. Anything not genuine is stored as unverified and goes no further.</li>
    <li><b>Match</b>A refund is Wardlock's own if its refund ID is in the ledger, or if its
      <code>custom_id</code> names a Wardlock decision with the same capture and the same amount.</li>
    <li><b>Alert</b>Anything else raises <code>OUT_OF_BAND_REFUND</code> in the merchant console.
      A reversal (chargeback) raises <code>CAPTURE_REVERSED</code>.</li>
  </ol>

  <h2>Events</h2>
  <dl class="defs">
    <dt>PAYMENT.CAPTURE.REFUNDED</dt><dd>Reconciled against the ledger, as above.</dd>
    <dt>PAYMENT.CAPTURE.REVERSED</dt><dd>Always alerted.</dd>
    <dt>anything else</dt><dd>Stored and acknowledged.</dd>
  </dl>

  <h2>Responses</h2>
  <dl class="defs">
    <dt>200</dt><dd>Processed (recorded, backfilled, alerted or ignored), or a duplicate of an event already processed.</dd>
    <dt>400</dt><dd>Not a PayPal event, missing signature headers, or the signature didn't verify.</dd>
    <dt>413</dt><dd>Larger than 256 KB. Real events are a few KB.</dd>
    <dt>503</dt><dd>Verification isn't configured, or PayPal couldn't be reached. PayPal delivers it again later.</dd>
  </dl>
</div>""")
