import { useState } from "react";
import Grid from "../Grid.jsx";
import { AuthError, api, when } from "../api.js";
import { useData } from "../useData.js";

const STATUSES = ["PENDING", "", "APPROVED", "EXECUTED", "DENIED", "EXPIRED", "BLOCKED"];

export default function Holds({ onAuthError }) {
  const [status, setStatus] = useState("PENDING");
  const [message, setMessage] = useState("");
  const { data, error, reload } = useData(status ? `/holds?status=${status}` : "/holds", onAuthError);

  async function decide(hold, action) {
    const verb = action === "approve" ? "Approve" : "Deny";
    if (!window.confirm(`${verb} the refund of ${hold.amount} ${hold.currency} for ${hold.customer_id}?`)) return;
    try {
      const result = await api(`/holds/${encodeURIComponent(hold.id)}/${action}`, { method: "POST" });
      setMessage(`${hold.id}: ${result.message}`);
    } catch (e) {
      if (e instanceof AuthError) return onAuthError();
      setMessage(`${hold.id}: ${e.message}`);
    }
    reload();
  }

  const columns = [
    { field: "created_at", headerName: "Held at", valueFormatter: (p) => when(p.value), width: 190 },
    {
      field: "ticket_id", headerName: "Ticket", width: 120,
      cellRenderer: (p) => <a href={`#/replay/${p.data.session_id}`}>{p.value}</a>,
    },
    { field: "customer_id", headerName: "Customer", width: 120 },
    { field: "amount", headerName: "Amount", width: 110, valueFormatter: (p) => `${p.value ?? ""} ${p.data.currency ?? ""}` },
    { field: "status", headerName: "Status", width: 110 },
    {
      field: "reasons", headerName: "Why it is held", flex: 1, wrapText: true, autoHeight: true,
      valueFormatter: (p) => (p.value || []).map((r) => r.explanation || r.code).join(" "),
    },
    {
      headerName: "Action", width: 190, sortable: false, filter: false,
      cellRenderer: (p) =>
        p.data.status === "PENDING" ? (
          <span className="actions">
            <button onClick={() => decide(p.data, "approve")}>Approve</button>
            <button className="danger" disabled={p.data.outcome_unknown}
              title={p.data.outcome_unknown ? "PayPal may already have refunded: only approval is safe" : ""}
              onClick={() => decide(p.data, "deny")}>Deny</button>
          </span>
        ) : (p.data.approver ? `by ${p.data.approver}` : ""),
    },
  ];

  return (
    <section>
      <div className="toolbar">
        <h2>Holds</h2>
        <label>
          Status
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            {STATUSES.map((s) => <option key={s} value={s}>{s || "All"}</option>)}
          </select>
        </label>
        <button onClick={reload}>Refresh</button>
      </div>
      {message && <p className="notice">{message}</p>}
      {error && <p className="error">{error}</p>}
      <Grid rows={data} columns={columns} />
    </section>
  );
}
