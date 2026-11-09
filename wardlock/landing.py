"""The page at "/": what this server is and where its parts live (static, no data, no secrets)."""

REPO_URL = "https://github.com/Harshit-Murarka/wardlock"

LANDING_HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wardlock</title>
<style>
  :root {{ --bg:#f7f7f5; --fg:#1d1d1f; --muted:#5f6368; --card:#fff; --line:#e3e3e0; --accent:#0b5cad; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#141416; --fg:#ececec; --muted:#a1a1a6; --card:#1e1e21; --line:#2e2e33; --accent:#6aa8ff; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg);
         font:16px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
  main {{ max-width:760px; margin:0 auto; padding:48px 16px 64px; }}
  h1 {{ font-size:2.2rem; margin:0 0 4px; letter-spacing:-0.02em; }}
  .tag {{ color:var(--muted); margin:0 0 28px; font-size:1.08rem; }}
  .badge {{ display:inline-block; font-size:.78rem; border:1px solid var(--line); border-radius:999px;
           padding:2px 10px; color:var(--muted); margin-bottom:18px; }}
  .grid {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fit, minmax(210px, 1fr)); margin:0 0 28px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }}
  .card b {{ display:block; margin-bottom:4px; }}
  .card span {{ color:var(--muted); font-size:.93rem; }}
  h2 {{ font-size:1.05rem; margin:28px 0 10px; }}
  table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line);
          border-radius:10px; overflow:hidden; font-size:.95rem; }}
  td {{ padding:10px 14px; border-top:1px solid var(--line); vertical-align:top; }}
  tr:first-child td {{ border-top:0; }}
  code {{ font:.9em ui-monospace, SFMono-Regular, Consolas, monospace; }}
  a {{ color:var(--accent); text-decoration:none; }}
  a:hover {{ text-decoration:underline; }}
  footer {{ margin-top:32px; color:var(--muted); font-size:.88rem; }}
</style>
</head>
<body>
<main>
  <span class="badge">PayPal AI Hackathon 2026 &middot; PayPal sandbox only</span>
  <h1>Wardlock</h1>
  <p class="tag">A money gate that lets an AI support agent refund PayPal customers on its own,
  without being able to refund the wrong person, the wrong amount, or behind your back.</p>

  <div class="grid">
    <div class="card"><b>G1 &middot; Ownership</b><span>Money only moves on orders of the customer of this
      conversation, taken from the helpdesk's verified ID, never from the ticket text.</span></div>
    <div class="card"><b>G2 &middot; Computed amounts</b><span>The agent picks refund handles; Wardlock computes
      every amount from order data. The refund tool has no amount field.</span></div>
    <div class="card"><b>G3 &middot; No bypass</b><span>Only Wardlock holds PayPal credentials. Signed webhooks
      flag any refund it did not make.</span></div>
  </div>

  <h2>On this server</h2>
  <table>
    <tr><td><code>/mcp</code></td><td>The agent's MCP server (streamable HTTP). Every call needs a session
      token: <code>Authorization: Bearer &lt;token&gt;</code>.</td></tr>
    <tr><td><a href="/console/"><code>/console/</code></a></td><td>Merchant console: holds to approve, alerts,
      decisions and ticket replay (password protected).</td></tr>
    <tr><td><a href="/docs"><code>/docs</code></a></td><td>HTTP API reference, including <code>POST /sessions</code>
      for the trusted helpdesk.</td></tr>
    <tr><td><code>/webhooks/paypal</code></td><td>Signed PayPal webhooks, reconciled against the ledger.</td></tr>
    <tr><td><a href="/health"><code>/health</code></a></td><td>Liveness check.</td></tr>
  </table>

  <footer>Source, design and evaluation: <a href="{REPO_URL}">{REPO_URL.removeprefix("https://")}</a>.
  This free server sleeps when idle; the first request after a pause takes about a minute.</footer>
</main>
</body>
</html>
"""
