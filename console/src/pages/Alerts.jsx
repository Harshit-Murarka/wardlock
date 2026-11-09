import Grid from "../Grid.jsx";
import { AuthError, api, when } from "../api.js";
import { useData } from "../useData.js";

export default function Alerts({ onAuthError, readOnly }) {
  const { data, error, reload } = useData("/alerts", onAuthError);

  async function acknowledge(alert) {
    try {
      await api(`/alerts/${encodeURIComponent(alert.id)}/ack`, { method: "POST" });
    } catch (e) {
      if (e instanceof AuthError) return onAuthError();
    }
    reload();
  }

  const columns = [
    { field: "created_at", headerName: "Time", valueFormatter: (p) => when(p.value), sort: "desc", width: 190 },
    { field: "kind", headerName: "Kind", width: 200 },
    {
      field: "details", headerName: "Details", flex: 1, wrapText: true, autoHeight: true,
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
        <span className="hint">Refunds PayPal reported that Wardlock did not make, and reversed captures.</span>
        <button onClick={reload}>Refresh</button>
      </div>
      {error && <p className="error">{error}</p>}
      <Grid rows={data} columns={columns} />
    </section>
  );
}
