import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import SuccessToast from "./SuccessToast";
import JsBarcode from "jsbarcode";
import QRCode from "qrcode";
import api, { post } from "../api";
import ExpandRowButton from "./ExpandRowButton";

const emptyFilters = { mawb: "", hawb: "", airlines_name: "", flight_no: "" };
const groupKey = (row) => JSON.stringify([row.mawb, row.hawb]);
const escapeHtml = (value) => String(value ?? "-").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const createdAtFormatter = new Intl.DateTimeFormat("id-ID", {
  dateStyle: "medium", timeStyle: "medium", timeZone: "Asia/Jakarta",
});

function createdAtTimestamp(value) {
  if (!value) return NaN;
  const datetime = value.replace(" ", "T");
  // SQLite CURRENT_TIMESTAMP is UTC and has no offset.
  return Date.parse(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(datetime) ? datetime : `${datetime}Z`);
}

function formatCreatedAt(value) {
  const timestamp = createdAtTimestamp(value);
  return Number.isFinite(timestamp) ? createdAtFormatter.format(timestamp) : "—";
}

async function printLabels(rows, parts, setError) {
  if (!rows.length) return;
  const win = window.open("", "_blank");
  if (!win) { setError("Print window blocked. Allow pop-ups and try again."); return; }
  try {
    const barcode = (value) => {
      if (!value) return "";
      const canvas = document.createElement("canvas");
      JsBarcode(canvas, String(value), { format: "CODE128", height: 26, width: 2, displayValue: false, margin: 0 });
      return canvas.toDataURL("image/png");
    };
    const pages = await Promise.all(rows.map(async (row) => {
      const part = parts.get(row.id);
      const total = row.pcs_hawb ?? row.pcs_mawb ?? 0;
      const qr = await QRCode.toDataURL(`${row.hawb}_${part}_${total}`, { margin: 0, width: 160, errorCorrectionLevel: "M" });
      return `<div class="page"><div class="head"><span>Airlines:<b>${escapeHtml(row.airlines_name)}</b></span><span>Flight No:<b>${escapeHtml(row.flight_no)}</b></span><span>Route:<b>${escapeHtml(row.route)}</b></span></div>
        <div class="sec"><strong>Air Waybill No:</strong><div class="row"><div class="left"><img class="bc" src="${barcode(row.mawb)}"><b>${escapeHtml(row.mawb)}</b></div><div>Pieces<br><b>${escapeHtml(row.pcs_hawb ?? row.pcs_mawb)}</b></div></div></div>
        <div class="sec"><strong>H. Waybill No:</strong><div class="row"><div class="left"><img class="bc" src="${barcode(row.hawb)}"><div class="hawb-line"><b>${escapeHtml(row.hawb)}</b><small>Pieces ${escapeHtml(part)}/${escapeHtml(row.pcs_hawb ?? row.pcs_mawb)}</small></div></div><img class="qr" src="${qr}"></div></div></div>`;
    }));
    win.document.write(`<!doctype html><html><head><title>Labels</title><style>
      @page { size: 100mm 50mm; margin: 0 } body { margin: 0; font-family: Helvetica, Arial, sans-serif; color: #111 }
      .page { width: 100mm; height: 50mm; box-sizing: border-box; border: 1px solid #111; padding: 2.5mm; break-after: page; display: flex; flex-direction: column }
      .page:last-child { break-after: auto } .head { display: flex; justify-content: space-between; border-bottom: 1.5px solid #111; padding-bottom: 1mm; font-size: 6.5pt }
      .head span { flex: 1; font-weight: bold } .head span:nth-child(2) { text-align: center } .head span:last-child { text-align: right } .head b { display: block; font-size: 7.5pt; font-weight: normal }
      .sec { flex: 1; display: flex; flex-direction: column; justify-content: center; font-size: 6.5pt } .sec + .sec { border-top: 1px dashed #999 } .sec strong { font-size: 7.5pt; text-transform: uppercase; letter-spacing: .3mm }
      .row { display: flex; justify-content: space-between; align-items: center } .left { flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: flex-start } .left b { font-size: 10pt } .hawb-line { display: flex; width: 72mm; max-width: 100%; align-items: baseline; justify-content: space-between; gap: 2mm } .hawb-line small { font-size: 6.5pt; white-space: nowrap } .bc { width: 72mm; max-width: 100%; height: auto; margin-top: 1mm } .qr { width: 16mm; height: 16mm }
    </style></head><body>${pages.join("")}</body></html>`);
    win.document.close();
    await Promise.all(Array.from(win.document.images, (img) => img.decode()));
    win.focus();
    win.print();
  } catch (error) {
    win.close();
    setError(`Could not prepare labels: ${error.message}`);
  }
}

export default function LabelsTab({ printOnly = false }) {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [importNotice, setImportNotice] = useState(null);
  const [filters, setFilters] = useState(emptyFilters);
  const [applied, setApplied] = useState(emptyFilters);
  const [grouped, setGrouped] = useState(true);
  const [expanded, setExpanded] = useState(new Set());
  const [selected, setSelected] = useState(new Set());
  const [page, setPage] = useState(1);
  const fileRef = useRef(null);
  const [importPjt, setImportPjt] = useState("");
  const [pjtCodes, setPjtCodes] = useState([]);

  const load = async () => {
    setLoading(true);
    try {
      const data = await api("/labels");
      const items = Array.isArray(data) ? data : data?.items;
      if (!Array.isArray(items)) throw new Error("Invalid labels response");
      const sorted = [...items].sort((a, b) => {
        const aTime = createdAtTimestamp(a.created_at);
        const bTime = createdAtTimestamp(b.created_at);
        const aOrder = Number.isFinite(aTime) ? aTime : -Infinity;
        const bOrder = Number.isFinite(bTime) ? bTime : -Infinity;
        return bOrder - aOrder || b.id - a.id;
      });
      setRows(sorted);
      setSelected((old) => new Set(items.filter((row) => old.has(row.id)).map((row) => row.id)));
      setError("");
      return true;
    } catch (e) { setError(`Could not load labels: ${e.message}`); return false; }
    finally { setLoading(false); }
  };
  useEffect(() => { void load(); }, []);
  useEffect(() => {
    if (printOnly) return;
    let active = true;
    api("/labels/pjt-codes").then((codes) => { if (active) setPjtCodes(codes); })
      .catch(() => { if (active) setError("Daftar PJT gagal dimuat."); });
    return () => { active = false; };
  }, [printOnly]);
  const closeImportNotice = useCallback(() => setImportNotice(null), []);

  const allGroups = new Map();
  for (const row of rows) {
    const key = groupKey(row);
    if (!allGroups.has(key)) allGroups.set(key, []);
    allGroups.get(key).push(row);
  }
  const parts = new Map();
  for (const members of allGroups.values()) {
    [...members].sort((a, b) => a.id - b.id).forEach((row, i) => parts.set(row.id, row.part ?? i + 1));
  }
  const filtered = rows.filter((row) => Object.entries(applied).every(([key, value]) => String(row[key] ?? "").toLowerCase().includes(value.trim().toLowerCase())));
  const groups = new Map();
  for (const row of filtered) {
    const key = groupKey(row);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  }
  const entries = grouped ? [...groups.entries()] : filtered.map((row) => [String(row.id), [row]]);
  const pages = Math.max(1, Math.ceil(entries.length / 20));
  const currentPage = Math.min(page, pages);
  const visible = entries.slice((currentPage - 1) * 20, currentPage * 20);
  const toggle = (id) => setSelected((old) => { const next = new Set(old); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const print = (items) => { setError(""); void printLabels(items, parts, setError); };
  const remove = async (items) => {
    if (!items.length || !window.confirm(`Delete ${items.length} label(s)?`)) return;
    setBusy(true); setError(""); setMessage("");
    try {
      await post("/labels/delete-many", { ids: items.map((row) => row.id) });
      if (await load()) setMessage("Labels deleted.");
    } catch (e) { setError(`Delete failed: ${e.message}`); }
    finally { setBusy(false); }
  };
  const upload = async (file) => {
    setImportNotice(null);
    setBusy(true); setError(""); setMessage("");
    try {
      const body = new FormData(); body.append("file", file);
      if (importPjt.trim()) body.append("pjt_code", importPjt.trim());
      const result = await api("/labels/import", { method: "POST", body });
      setImportNotice(result);
      await load();
    } catch (e) { setError(`Import failed: ${e.message}`); }
    finally { setBusy(false); }
  };
  const selectedRows = rows.filter((row) => selected.has(row.id));
  const rowCells = (row) => <><td>{parts.get(row.id)}</td><td>{row.mawb}</td><td>{row.pcs_mawb ?? "-"}</td><td>{row.hawb}</td><td>{row.pcs_hawb ?? "-"}</td><td>{row.airlines_name ?? "-"}</td><td>{row.flight_no ?? "-"}</td><td>{row.route ?? "-"}</td><td className="whitespace-nowrap">{formatCreatedAt(row.created_at)}</td></>;
  const actions = (items) => <div className="label-actions">
    <button className="label-action label-action-print" onClick={() => print(items)}>
      <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M6 9V3h12v6M6 18H4V9h16v9h-2M6 14h12v7H6z" /><path d="M17 11h1" /></svg>Print
    </button>
    {!printOnly && <button className="label-action label-action-delete" disabled={busy} onClick={() => remove(items)}>
      <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M4 7h16M9 7V4h6v3m3 0-1 13H7L6 7m4 4v5m4-5v5" /></svg>Delete
    </button>}
  </div>;
  const individual = (row) => <tr key={row.id}><td><input type="checkbox" aria-label={`Select label ${row.id}`} checked={selected.has(row.id)} onChange={() => toggle(row.id)} /></td>{rowCells(row)}<td>{actions([row])}</td></tr>;

  return <div className="data-workspace label-workspace grid min-w-0 grid-cols-[minmax(0,1fr)] gap-3">
    {importNotice && <SuccessToast title="Impor Excel berhasil" onClose={closeImportNotice}>
      {importNotice.imported ?? 0} ditambahkan · {importNotice.updated ?? 0} diperbarui · {importNotice.skipped ?? 0} dilewati
    </SuccessToast>}
    <section className="panel">
      <div className="awb-section-heading">
        <div>
          <h3>Print Label</h3>
          <p>{rows.length} label · 100 × 50 mm</p>
        </div>
        {!printOnly && <div className="flex flex-wrap items-center gap-3">
          <input ref={fileRef} type="file" accept=".xlsx,.xls" className="hidden"
            onChange={(e) => { const file = e.target.files?.[0]; e.target.value = ""; if (file) void upload(file); }} />
          <label className="text-xs font-bold">Kode PJT (opsional)
            <select className="field mt-1" value={importPjt} disabled={busy}
              onChange={(event) => setImportPjt(event.target.value)}>
              <option value="">Internal</option>
              {pjtCodes.map((code) => <option key={code} value={code}>{code}</option>)}
            </select>
          </label>
          <a className="btn-ghost" href="/api/labels/template">Download Template</a>
          <button type="button" className="btn-clay" disabled={busy} onClick={() => fileRef.current?.click()}>
            {busy ? "Processing..." : "Import Excel"}
          </button>
        </div>}
      </div>
      {error && <p role="alert" className="text-red-400 text-sm mt-4">{error}</p>}{message && <p role="status" className="st-saved text-sm mt-4">{message}</p>}
    </section>
    <section className="panel"><div className="flex flex-wrap items-center justify-between gap-3"><h3>Display Mode</h3><div className="flex flex-wrap gap-2"><button className={grouped ? "btn-clay" : "btn-ghost"} aria-pressed={grouped} onClick={() => { setGrouped(true); setPage(1); }}>Grouping</button><button className={!grouped ? "btn-clay" : "btn-ghost"} aria-pressed={!grouped} onClick={() => { setGrouped(false); setPage(1); }}>Non-Grouping</button></div></div>
      <form onSubmit={(e) => { e.preventDefault(); setApplied(filters); setPage(1); }}><div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 mt-4">{[["mawb", "MAWB"], ["hawb", "HAWB"], ["airlines_name", "Airlines"], ["flight_no", "Flight"]].map(([key, label]) => <label key={key} className="text-xs font-bold">{label}<input className="field mt-1" list={`label-filter-${key}`} value={filters[key]} onChange={(e) => setFilters({ ...filters, [key]: e.target.value })} /><datalist id={`label-filter-${key}`}>{[...new Set(rows.map((row) => row[key]).filter(Boolean))].sort().map((value) => <option key={value} value={value} />)}</datalist></label>)}</div><div className="flex gap-3 mt-4"><button className="btn-clay" type="submit">Apply Filters</button><button className="btn-ghost" type="button" onClick={() => { setFilters(emptyFilters); setApplied(emptyFilters); setPage(1); }}>Reset</button></div></form>
    </section>
    <section className="panel">
      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3 mb-4">
        <button type="button" className="btn-ghost" disabled={!filtered.length} onClick={() => print(filtered)}>
          Print All Filtered
        </button>
        <div className="flex flex-wrap items-center gap-3">
          <button type="button" className="btn-ghost" disabled={!selectedRows.length} onClick={() => print(selectedRows)}>
            Print Selected ({selectedRows.length})
          </button>
          {!printOnly && <button type="button" className="awb-delete" disabled={!selectedRows.length || busy} onClick={() => remove(selectedRows)}>
            Delete Selected
          </button>}
        </div>
      </div>
      <div className="awb-table-scroll" tabIndex={0} role="region" aria-label="Tabel label, dapat digeser horizontal"><table className="awb-data-table w-full"><thead><tr><th><input type="checkbox" aria-label="Select all filtered labels" checked={filtered.length > 0 && filtered.every((row) => selected.has(row.id))} onChange={(e) => setSelected((old) => { const next = new Set(old); filtered.forEach((row) => e.target.checked ? next.add(row.id) : next.delete(row.id)); return next; })} /></th><th>Part</th><th>MAWB</th><th>MAWB PCS</th><th>HAWB</th><th>HAWB PCS</th><th>Airlines</th><th>Flight</th><th>Route</th><th>Created At (WIB)</th><th>Actions</th></tr></thead><tbody>
      {loading ? <tr><td colSpan="11">Loading labels...</td></tr> : !filtered.length ? <tr><td colSpan="11">No labels found.</td></tr> :
        visible.map(([key, members]) => {
          const first = members[0];
          if (!grouped) return individual(first);
          const isExpanded = expanded.has(key);
          const toggleGroup = () => setExpanded((previous) => {
            const next = new Set(previous);
            if (next.has(key)) next.delete(key);
            else next.add(key);
            return next;
          });
          return <Fragment key={key}>
            <tr className={isExpanded ? "cursor-pointer font-bold" : "cursor-pointer"} onClick={toggleGroup}>
              <td><ExpandRowButton expanded={isExpanded}
                label={`${isExpanded ? "Collapse" : "Expand"} group ${first.hawb}`} onToggle={toggleGroup} /></td>
              <td>{members.length} parts</td><td>{first.mawb}</td><td>{first.pcs_mawb ?? "-"}</td>
              <td>{first.hawb}</td><td>{first.pcs_hawb ?? "-"}</td><td>{first.airlines_name ?? "-"}</td>
              <td>{first.flight_no ?? "-"}</td><td>{first.route ?? "-"}</td>
              <td className="whitespace-nowrap">{formatCreatedAt(first.created_at)}</td>
              <td onClick={(event) => event.stopPropagation()}>{actions(members)}</td>
            </tr>
            {isExpanded && members.map(individual)}
          </Fragment>;
        })}
    </tbody></table></div><div className="flex flex-wrap items-center justify-between gap-3 mt-4 text-xs muted"><span>{filtered.length} labels, {grouped ? `${groups.size} groups` : "individual view"}</span><div className="flex items-center gap-3"><button className="btn-ghost !py-1 !px-3" disabled={currentPage <= 1} onClick={() => setPage(currentPage - 1)}>Prev</button><span>{currentPage} / {pages}</span><button className="btn-ghost !py-1 !px-3" disabled={currentPage >= pages} onClick={() => setPage(currentPage + 1)}>Next</button></div></div></section>
  </div>;
}
