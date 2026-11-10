import Grid, { Verdict, money } from "../Grid.jsx";
import { when } from "../api.js";
import { useData } from "../useData.js";

export default function Ledger({ onAuthError }) {
  const { data, error, reload } = useData("/ledger", onAuthError);
  const columns = [
    { field: "created_at", headerName: "Time", valueFormatter: (p) => when(p.value), sort: "desc", width: 190 },
    {
      field: "ticket_id", headerName: "Ticket", width: 120,
      cellRenderer: (p) => <a href={`#/replay/${p.data.session_id || ""}`}>{p.value}</a>,
    },
    { field: "customer_id", headerName: "Customer", width: 120 },
    { field: "amount", headerName: "Amount", width: 120, cellClass: "num", valueFormatter: (p) => money(p.value, p.data.currency) },
    { field: "status", headerName: "PayPal status", width: 150, cellRenderer: (p) => <Verdict value={p.value} /> },
    { field: "capture_id", headerName: "Capture", width: 190 },
    { field: "paypal_refund_id", headerName: "PayPal refund", flex: 1 },
  ];
  return (
    <section>
      <div className="toolbar">
        <h2>Ledger</h2>
        <span className="hint">Every refund Wardlock sent to PayPal, with PayPal's own refund ID.</span>
        <span className="spacer" />
        <button onClick={reload}>Refresh</button>
      </div>
      {error && <p className="error">{error}</p>}
      <Grid rows={data} columns={columns} />
    </section>
  );
}
