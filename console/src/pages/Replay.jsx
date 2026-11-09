import Grid, { Verdict } from "../Grid.jsx";
import { when } from "../api.js";
import { useData } from "../useData.js";

// SPEC §12 ticket replay. The ticket is customer-written: it is shown ONLY as text nodes,
// never as HTML, so hidden comments and tags stay visible and can't run.
export default function Replay({ arg, onAuthError }) {
  return arg ? <OneSession id={arg} onAuthError={onAuthError} /> : <SessionList onAuthError={onAuthError} />;
}

function SessionList({ onAuthError }) {
  const { data, error } = useData("/sessions", onAuthError);
  const columns = [
    { field: "created_at", headerName: "Opened", valueFormatter: (p) => when(p.value), sort: "desc", width: 190 },
    { field: "ticket_id", headerName: "Ticket", width: 140, cellRenderer: (p) => <a href={`#/replay/${p.data.id}`}>{p.value}</a> },
    { field: "customer_id", headerName: "Customer", width: 140 },
    { field: "status", headerName: "Session", width: 120 },
    { field: "refund_calls", headerName: "Refund calls", flex: 1 },
  ];
  return (
    <section>
      <div className="toolbar">
        <h2>Ticket replay</h2>
        <span className="hint">Pick a ticket to see what the customer wrote, what the agent asked for, and what Wardlock decided.</span>
      </div>
      {error && <p className="error">{error}</p>}
      <Grid rows={data} columns={columns} />
    </section>
  );
}

function OneSession({ id, onAuthError }) {
  const { data, error } = useData(`/sessions/${encodeURIComponent(id)}/replay`, onAuthError);
  if (error) return <p className="error">{error}</p>;
  if (!data) return <p>Loading…</p>;
  const { session, ticket, calls } = data;

  return (
    <section>
      <div className="toolbar">
        <h2>Ticket {session.ticket_id}</h2>
        <span className="hint">customer {session.customer_id} · opened {when(session.created_at)} · session {session.status}</span>
        <a href="#/replay">All tickets</a>
      </div>
      <div className="replay">
        <div className="column">
          <h3>1. What the customer wrote</h3>
          <p className="hint">Untrusted text. Highlighted parts look like instructions to the agent.</p>
          <pre className="ticket">
            {ticket.map((segment, i) =>
              segment.suspicious ? (
                <mark key={i} title={segment.why}>{segment.text}</mark>
              ) : (
                <span key={i}>{segment.text}</span>
              ),
            )}
          </pre>
          {ticket.some((s) => s.suspicious) && (
            <ul className="flags">
              {ticket.filter((s) => s.suspicious).map((s, i) => <li key={i}>{s.why}</li>)}
            </ul>
          )}
        </div>
        <div className="column">
          <h3>2. What the agent asked for</h3>
          {calls.length === 0 && <p className="hint">The agent made no refund request.</p>}
          {calls.map((call) => (
            <div className="card" key={call.decision_id}>
              <div className="card-title">{call.tool} <span className="hint">{when(call.created_at)}</span></div>
              <pre>{JSON.stringify(call.request, null, 2)}</pre>
            </div>
          ))}
        </div>
        <div className="column">
          <h3>3. What Wardlock decided</h3>
          {calls.length === 0 && <p className="hint">Nothing to decide: no refund was requested.</p>}
          {calls.map((call) => (
            <div className="card" key={call.decision_id}>
              <div className="card-title">
                <Verdict value={call.verdict} /> {call.amount ? `${call.amount} ${call.currency}` : "no amount"}
              </div>
              {call.reasons.length === 0 && <p>All checks passed.</p>}
              <ul>
                {call.reasons.map((r) => (
                  <li key={r.code}><code>{r.code}</code> {r.explanation}</li>
                ))}
              </ul>
              {call.reason_check && (
                <p className="hint">
                  Reason check: {String(call.reason_check.consistent)}
                  {call.reason_check.rationale ? `: ${call.reason_check.rationale}` : ""}
                  {call.reason_check.error ? ` (${call.reason_check.error})` : ""}
                </p>
              )}
              {call.hold && <p>Hold <a href="#/holds">{call.hold.id}</a>: {call.hold.status}</p>}
              {call.refund && <p>Refunded: {call.refund.status} ({call.refund.id})</p>}
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
