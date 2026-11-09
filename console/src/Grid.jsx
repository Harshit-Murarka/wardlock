import { AgGridReact } from "ag-grid-react";

// AG Grid renders values as text unless a cell renderer says otherwise, so customer-written
// strings can never become HTML here.
export default function Grid({ rows, columns, height = 520 }) {
  return (
    <div className="ag-theme-quartz grid" style={{ height }}>
      <AgGridReact
        rowData={rows || []}
        columnDefs={columns}
        defaultColDef={{ sortable: true, filter: true, resizable: true }}
        pagination={true}
        paginationPageSize={50}
        overlayNoRowsTemplate="Nothing here yet."
      />
    </div>
  );
}

export function Verdict({ value }) {
  return <span className={`badge ${String(value || "").toLowerCase()}`}>{value}</span>;
}
