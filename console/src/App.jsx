import { useCallback, useEffect, useState } from "react";
import { AuthError, VIEWER, api, consoleConfig, getPassword, isViewer, setPassword } from "./api.js";
import Decisions from "./pages/Decisions.jsx";
import Holds from "./pages/Holds.jsx";
import Alerts from "./pages/Alerts.jsx";
import Ledger from "./pages/Ledger.jsx";
import Replay from "./pages/Replay.jsx";

const PAGES = [
  ["decisions", "Decisions"],
  ["holds", "Holds"],
  ["alerts", "Alerts"],
  ["ledger", "Ledger"],
  ["replay", "Ticket replay"],
];

// #/holds, #/replay/ses_123 ...
function readRoute() {
  const [page = "decisions", arg = null] = window.location.hash.replace(/^#\/?/, "").split("/");
  return { page: PAGES.some(([key]) => key === page) ? page : "decisions", arg };
}

function Login({ onDone }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const [demoOpen, setDemoOpen] = useState(false);

  useEffect(() => {
    consoleConfig().then((config) => setDemoOpen(Boolean(config.public_readonly)));
  }, []);

  async function signIn(password, user) {
    setPassword(password, user);
    try {
      await api("/alerts");
      onDone();
    } catch (e) {
      setPassword(null);
      setError(e instanceof AuthError ? "Wrong password." : e.message);
    }
  }

  function submit(event) {
    event.preventDefault();
    signIn(value, "admin");
  }

  return (
    <form className="login" onSubmit={submit}>
      <h1>Wardlock console</h1>
      <label>
        Console password
        <input type="password" value={value} onChange={(e) => setValue(e.target.value)} autoFocus />
      </label>
      <button type="submit" disabled={!value}>Sign in</button>
      {error && <p className="error">{error}</p>}
      {demoOpen && (
        <div className="demo-entry">
          <span>Judging or just curious?</span>
          <button type="button" className="primary" onClick={() => signIn("demo", VIEWER)}>
            View the live demo (read-only)
          </button>
          <small>See every decision, hold, alert and ticket replay. Approving or denying still needs the
            merchant's password.</small>
        </div>
      )}
    </form>
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
        <strong>Wardlock</strong>
        <nav>
          {PAGES.map(([key, label]) => (
            <a key={key} href={`#/${key}`} className={route.page === key ? "active" : ""}>
              {label}
            </a>
          ))}
        </nav>
        {readOnly && <span className="badge">read-only demo</span>}
        <button className="link" onClick={() => { setPassword(null); setSignedIn(false); }}>Sign out</button>
      </header>
      {readOnly && (
        <p className="readonly-note">
          You're looking at the live demo data read-only. Start with <a href="#/replay">Ticket replay</a> to
          see a customer's message, what the AI agent asked for, and why Wardlock allowed, held or blocked it.
        </p>
      )}
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
