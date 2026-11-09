import { useCallback, useEffect, useState } from "react";
import { AuthError, api, getPassword, setPassword } from "./api.js";
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

  async function submit(event) {
    event.preventDefault();
    setPassword(value);
    try {
      await api("/alerts");
      onDone();
    } catch (e) {
      setError(e instanceof AuthError ? "Wrong password." : e.message);
    }
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
  const props = { arg: route.arg, onAuthError };

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
        <button className="link" onClick={() => { setPassword(null); setSignedIn(false); }}>Sign out</button>
      </header>
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
