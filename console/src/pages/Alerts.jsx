import Grid from "../Grid.jsx";
import { useState } from "react";
import { AuthError, api, when } from "../api.js";
import { useData } from "../useData.js";

export default function Alerts({ onAuthError, readOnly }) {
  const { data, error, reload } = useData("/alerts", onAuthError);
  const [failure, setFailure] = useState("");

  async function acknowledge(alert) {
    setFailure("");
    try {
      await api(`/alerts/${encodeURIComponent(alert.id)}/ack`, { method: "POST" });
    } catch (e) {
      if (e instanceof AuthError) return onAuthError();
      setFailure(`Could not acknowledge the alert: ${e.message}`);
    }
    reload();
  }

  const columns = [
    { field: "created_at", headerName: "Time", valueFormatter: (p) => when(p.value), sort: "desc", width: 190 },
    { field: "kind", headerName: "Kind", width: 220, cellRenderer: (p) => <span className="badge block">{String(p.value).replaceAll("_", " ")}</span> },
    {
      field: "details", headerName: "Details", flex: 1, minWidth: 280, wrapText: true, autoHeight: true,
      valueFormatter: (p) => Object.entries(p.value || {}).map(([k, v]) => `${k}: ${v}`).join(" · "),
    },
    {
      field: "acknowledged", headerName: "", width: 150, sortable: false, filter: false,
      cellRenderer: (p) => (p.value ? "acknowledged" : readOnly ? <span className="hint">open</span>
        : <button onClick={() => acknowledge(p.data)}>Acknowledge</button>),
    },
  ];

  return (
    <section>
      <div className="toolbar">
        <h2>Alerts</h2>
        <span className="hint">Refunds PayPal reported that Wardlock did not make, and reversed payments.</span>
        <span className="spacer" />
        <button onClick={reload}>Refresh</button>
      </div>
      {(error || failure) && <p className="error" role="alert">{error || failure}</p>}
      <Grid rows={data} columns={columns} />
    </section>
  );
}
