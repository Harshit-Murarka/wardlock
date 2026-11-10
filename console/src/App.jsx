import { useCallback, useEffect, useState } from "react";
import { AuthError, VIEWER, api, consoleConfig, getPassword, isViewer, setPassword } from "./api.js";
import { useData } from "./useData.js";
import Decisions from "./pages/Decisions.jsx";
import Holds from "./pages/Holds.jsx";
import Alerts from "./pages/Alerts.jsx";
import Ledger from "./pages/Ledger.jsx";
import Replay from "./pages/Replay.jsx";

const PAGES = [
  ["replay", "ticket replay"],
  ["decisions", "decisions"],
  ["holds", "holds"],
  ["alerts", "alerts"],
  ["ledger", "ledger"],
];
const HOME = "replay"; // the page that explains the product best, so it opens first

// #/holds, #/replay/ses_123 ...
function readRoute() {
  const [page = HOME, arg = null] = window.location.hash.replace(/^#\/?/, "").split("/");
  return { page: PAGES.some(([key]) => key === page) ? page : HOME, arg };
}

function Login({ onDone }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [demoOpen, setDemoOpen] = useState(false);

  useEffect(() => {
    consoleConfig().then((config) => setDemoOpen(Boolean(config.public_readonly)));
  }, []);

  async function signIn(password, user) {
    setBusy(true);
    setError("");
    setPassword(password, user);
    try {
      await api("/summary");
      onDone();
    } catch (e) {
      setPassword(null);
      setError(e instanceof AuthError ? "That password isn't right." : e.message);
    } finally {
      setBusy(false);
    }
  }

  function submit(event) {
    event.preventDefault();
    signIn(value, "admin");
  }

  return (
    <form className="login" onSubmit={submit}>
      <a className="mark" href="/">wardlock<span> / console</span></a>
      <h1>Merchant console</h1>
      <p>Held refunds to approve or deny, alerts for refunds Wardlock didn't make, and a replay of every ticket.</p>
      {demoOpen && (
        <div className="demo-entry">
          <strong>Judging, or just looking?</strong>
          <button type="button" className="primary" disabled={busy} onClick={() => signIn("demo", VIEWER)}>
            View the live demo (read-only)
          </button>
          <small>No password. You see everything the merchant sees; approving or denying a refund still needs
            the merchant's password, and the server enforces that.</small>
        </div>
      )}
      <label>
        Merchant password
        <input type="password" value={value} onChange={(e) => setValue(e.target.value)}
          autoComplete="current-password" autoFocus={!demoOpen} />
      </label>
      <button type="submit" disabled={!value || busy}>Sign in</button>
      {error && <p className="error" role="alert">{error}</p>}
      <footer><a href="/">What is Wardlock?</a></footer>
    </form>
  );
}

// The totals under the top bar. Each one links to the page that lists what it counts.
function Summary({ page, onAuthError }) {
  const { data, reload } = useData("/summary", onAuthError);
  useEffect(() => {
    reload(); // a page change is a good moment to recount
  }, [page, reload]);
  if (!data) return null;
  const refunded = data.refunded.length
    ? data.refunded.map((r) => `${r.amount} ${r.currency}`).join(" + ")
    : "0.00";
  return (
    <div className="summary" aria-label="Totals">
      <a href="#/replay"><b>{data.tickets}</b><span>tickets</span></a>
      <a href="#/decisions" className="allow"><b>{data.allowed}</b><span>allowed</span></a>
      <a href="#/decisions" className="hold"><b>{data.held}</b><span>held</span></a>
      <a href="#/decisions" className="block"><b>{data.blocked}</b><span>blocked</span></a>
      <a href="#/holds"><b>{data.pending_holds}</b><span>waiting for you</span></a>
      <a href="#/alerts"><b>{data.open_alerts}</b><span>open alerts</span></a>
      <a href="#/ledger"><b>{refunded}</b><span>refunded</span></a>
    </div>
  );
}

export default function App() {
  const [route, setRoute] = useState(readRoute);
  const [signedIn, setSignedIn] = useState(Boolean(getPassword()));

  useEffect(() => {
    const onHash = () => setRoute(readRoute());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  // Stable identity, so pages don't reload their data on every render.
  const onAuthError = useCallback(() => setSignedIn(false), []);

  if (!signedIn) return <Login onDone={() => setSignedIn(true)} />;
  const readOnly = isViewer();
  const props = { arg: route.arg, onAuthError, readOnly };

  return (
    <div className="app">
      <header>
        <a className="mark" href="/">wardlock<span> / console</span></a>
        <nav>
          {PAGES.map(([key, label]) => (
            <a key={key} href={`#/${key}`} className={route.page === key ? "active" : ""}
              aria-current={route.page === key ? "page" : undefined}>
              {label}
            </a>
          ))}
        </nav>
        {readOnly && <span className="badge">read-only demo</span>}
        <button className="link" onClick={() => { setPassword(null); setSignedIn(false); }}>sign out</button>
      </header>
      {readOnly && (
        <p className="readonly-note">
          This is the live demo data, read-only. Open a ticket below to see what the customer wrote, what the AI
          agent asked for, and why Wardlock allowed, held or blocked it.
        </p>
      )}
      <Summary page={route.page} onAuthError={onAuthError} />
      <main>
        {route.page === "decisions" && <Decisions {...props} />}
        {route.page === "holds" && <Holds {...props} />}
        {route.page === "alerts" && <Alerts {...props} />}
        {route.page === "ledger" && <Ledger {...props} />}
        {route.page === "replay" && <Replay {...props} />}
      </main>
    </div>
  );
}
