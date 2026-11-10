import Grid, { Verdict, money } from "../Grid.jsx";
import { when } from "../api.js";
import { useData } from "../useData.js";

// SPEC §12 ticket replay. The ticket is customer-written: it is shown ONLY as text nodes,
// never as HTML, so hidden comments and tags stay visible and can't run.
export default function Replay({ arg, onAuthError }) {
  return arg ? <OneSession id={arg} onAuthError={onAuthError} /> : <SessionList onAuthError={onAuthError} />;
}

function Outcome({ data }) {
  if (!data.refund_calls) return <span className="hint">no refund requested</span>;
  return (
    <span className="actions">
      {data.allowed > 0 && <span className="badge allow">{data.allowed} allowed</span>}
      {data.held > 0 && <span className="badge hold">{data.held} held</span>}
      {data.blocked > 0 && <span className="badge block">{data.blocked} blocked</span>}
    </span>
  );
}

function SessionList({ onAuthError }) {
  const { data, error } = useData("/sessions", onAuthError);
  const columns = [
    { field: "created_at", headerName: "Opened", valueFormatter: (p) => when(p.value), sort: "desc", width: 190 },
    { field: "ticket_id", headerName: "Ticket", width: 120, cellRenderer: (p) => <a href={`#/replay/${p.data.id}`}>{p.value}</a> },
    { field: "customer_id", headerName: "Customer", width: 130 },
    {
      field: "refund_calls", headerName: "What happened", width: 230,
      cellRenderer: (p) => <Outcome data={p.data} />,
    },
    { field: "preview", headerName: "The customer wrote", flex: 1, minWidth: 240 },
  ];
  return (
    <section>
      <div className="toolbar">
        <h2>Ticket replay</h2>
        <span className="hint">Open a ticket to read it next to what the agent asked for and what Wardlock decided.</span>
      </div>
      {error && <p className="error">{error}</p>}
      <Grid rows={data} columns={columns} />
    </section>
  );
}

// One line of a slip: key ....... value
function Row({ k, v }) {
  if (v.length > 34) {
    // Too long for one line (a note, a list of handles): the value goes underneath, in full.
    return <div className="row long"><span className="k">{k}</span><span className="v">{v}</span></div>;
  }
  return (
    <div className="row">
      <span className="k">{k}</span><span className="dots" /><span className="v">{v}</span>
    </div>
  );
}

function show(value) {
  if (value === null || value === undefined || value === "") return "none";
  return Array.isArray(value) ? value.join(", ") || "none" : String(value);
}

function Request({ call }) {
  return (
    <div className="card">
      <div className="card-title"><strong>{call.tool}</strong><span>{when(call.created_at)}</span></div>
      {Object.entries(call.request || {}).map(([key, value]) => <Row key={key} k={key} v={show(value)} />)}
    </div>
  );
}

function Decision({ call }) {
  const check = call.reason_check;
  return (
    <div className="card">
      <div className="card-title">
        <Verdict value={call.verdict} />
        <span className="amount">{money(call.amount, call.currency) || "no amount"}</span>
      </div>
      {call.reasons.length === 0 && <p>Every check passed.</p>}
      <ul>
        {call.reasons.map((r) => (
          <li key={r.code}><code>{r.code}</code>{r.explanation}</li>
        ))}
      </ul>
      {(check || call.hold || call.refund) && (
        <div className="after">
          {check && (
            <p>
              reason check: {check.consistent === true ? "fits the ticket" : check.consistent === false ? "does not fit the ticket" : "no answer"}
              {check.rationale ? ` (${check.rationale})` : ""}
              {check.error ? ` (${check.error})` : ""}
            </p>
          )}
          {call.hold && <p>hold <a href="#/holds">{call.hold.id}</a>: {call.hold.status}</p>}
          {call.refund && <p>refund {call.refund.id}: {call.refund.status}</p>}
        </div>
      )}
    </div>
  );
}

function OneSession({ id, onAuthError }) {
  const { data, error } = useData(`/sessions/${encodeURIComponent(id)}/replay`, onAuthError);
  if (error) return <p className="error">{error} <a href="#/replay">All tickets</a></p>;
  if (!data) return <p className="hint">Loading…</p>;
  const { session, ticket, calls } = data;
  const flagged = ticket.filter((s) => s.suspicious);

  return (
    <section>
      <div className="toolbar">
        <h2>Ticket {session.ticket_id}</h2>
        <span className="hint">{session.customer_id} · opened {when(session.created_at)} · session {String(session.status).toLowerCase()}</span>
        <span className="spacer" />
        <a href="#/replay">← all tickets</a>
      </div>
      <div className="replay">
        <div className="column">
          <h3><b>01</b>What the customer wrote</h3>
          <p className="hint">Untrusted text. Anything highlighted reads like an instruction to the agent.</p>
          <pre className="ticket">
            {ticket.map((segment, i) =>
              segment.suspicious ? (
                <mark key={i} title={segment.why}>{segment.text}</mark>
              ) : (
                <span key={i}>{segment.text}</span>
              ),
            )}
          </pre>
          {flagged.length > 0 && (
            <ul className="flags">
              {[...new Set(flagged.map((s) => s.why))].map((why) => <li key={why}>{why}</li>)}
            </ul>
          )}
        </div>
        <div className="column">
          <div className="pair-head">
            <h3><b>02</b>What the agent asked for</h3>
            <h3><b>03</b>What Wardlock decided</h3>
          </div>
          {calls.length === 0 && (
            <p className="card empty">The agent made no refund request on this ticket, so there was nothing to decide.</p>
          )}
          {calls.map((call) => (
            <div className="pair" key={call.decision_id}>
              <Request call={call} />
              <Decision call={call} />
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
