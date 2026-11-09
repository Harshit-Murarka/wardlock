import { useMemo, useState } from "react";
import Grid, { Verdict } from "../Grid.jsx";
import { when } from "../api.js";
import { useData } from "../useData.js";

const VERDICTS = ["", "ALLOW", "HOLD", "BLOCK"];

export default function Decisions({ onAuthError }) {
  const [verdict, setVerdict] = useState("");
  const [reason, setReason] = useState("");
  const query = new URLSearchParams();
  if (verdict) query.set("verdict", verdict);
  if (reason) query.set("reason_code", reason);
  const { data, error, reload } = useData(`/decisions?${query}`, onAuthError);
  const all = useData("/decisions", onAuthError).data;

  const reasons = useMemo(
    () => [...new Set((all || []).flatMap((d) => d.reason_codes))].sort(),
    [all],
  );
  const columns = [
    { field: "created_at", headerName: "Time", valueFormatter: (p) => when(p.value), sort: "desc", width: 190 },
    {
      field: "ticket_id", headerName: "Ticket", width: 130,
      cellRenderer: (p) => <a href={`#/replay/${p.data.session_id}`}>{p.value}</a>,
    },
    { field: "customer_id", headerName: "Customer", width: 130 },
    { field: "tool", headerName: "Tool", width: 170 },
    { field: "amount", headerName: "Amount", width: 110, valueFormatter: (p) => (p.value ? `${p.value} ${p.data.currency}` : "") },
    { field: "verdict", headerName: "Verdict", width: 110, cellRenderer: (p) => <Verdict value={p.value} /> },
    { field: "reason_codes", headerName: "Reason codes", flex: 1, valueFormatter: (p) => (p.value || []).join(", ") },
    { field: "latency_ms", headerName: "Latency (ms)", width: 130 },
  ];

  return (
    <section>
      <div className="toolbar">
        <h2>Decisions</h2>
        <label>
          Verdict
          <select value={verdict} onChange={(e) => setVerdict(e.target.value)}>
            {VERDICTS.map((v) => <option key={v} value={v}>{v || "All"}</option>)}
          </select>
        </label>
        <label>
          Reason code
          <select value={reason} onChange={(e) => setReason(e.target.value)}>
            <option value="">All</option>
            {reasons.map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
        </label>
        <button onClick={reload}>Refresh</button>
      </div>
      {error && <p className="error">{error}</p>}
      <Grid rows={data} columns={columns} />
    </section>
  );
}
