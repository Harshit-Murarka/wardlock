import { AgGridReact } from "ag-grid-react";

// AG Grid renders values as text unless a cell renderer says otherwise, so customer-written
// strings can never become HTML here.
export default function Grid({ rows, columns, height }) {
  return (
    <div className="ag-theme-quartz grid" style={height ? { height } : undefined}>
      <AgGridReact
        rowData={rows || []}
        columnDefs={columns}
        defaultColDef={{ sortable: true, filter: true, resizable: true }}
        pagination={true}
        paginationPageSize={50}
        loading={rows === null}
        overlayNoRowsTemplate="Nothing here yet."
      />
    </div>
  );
}

// ALLOW / HOLD / BLOCK, and hold or refund statuses, drawn as a small stamp.
export function Verdict({ value }) {
  if (!value) return null;
  return <span className={`badge ${String(value).toLowerCase()}`}>{value}</span>;
}

export const money = (amount, currency) => (amount ? `${amount} ${currency || ""}`.trim() : "");
