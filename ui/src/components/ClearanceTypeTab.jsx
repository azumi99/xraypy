import { useCallback, useEffect, useRef, useState } from "react";
import api from "../api";
import SuccessToast from "./SuccessToast";

const columns = [
  ["mawb", "MAWB"],
  ["hawb", "HAWB"],
  ["clearance_type", "Clearance Type"],
];
const initialFilters = { mawb: "", hawb: "", clearance_type: "" };
const pageSize = 20;

export default function ClearanceTypeTab() {
  const fileInput = useRef(null);
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(null);
  const [filters, setFilters] = useState(initialFilters);
  const [appliedFilters, setAppliedFilters] = useState(initialFilters);
  const [page, setPage] = useState(1);
  const closeNotice = useCallback(() => {
    setNotice((current) => current === notice ? null : current);
  }, [notice]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setRows(await api("/clearance-types"));
    } catch {
      setNotice({ title: "Data gagal dimuat", message: "Muat ulang halaman untuk mencoba lagi.", error: true });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const upload = async (file) => {
    setBusy(true);
    setNotice(null);
    try {
      const body = new FormData();
      body.append("file", file);
      const response = await fetch("/api/clearance-types/import", { method: "POST", body });
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Periksa file Excel.");
      setNotice({ title: "Impor Excel berhasil", message: `${result.saved} data disimpan.` });
      setPage(1);
      await load();
    } catch (error) {
      setNotice({ title: "Impor Excel gagal", message: error.message, error: true });
    } finally {
      setBusy(false);
    }
  };

  const filteredRows = rows.filter((row) => columns.every(([key]) =>
    row[key].toLowerCase().includes(appliedFilters[key].trim().toLowerCase()),
  ));
  const totalPages = Math.max(1, Math.ceil(filteredRows.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const visibleRows = filteredRows.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  return <div className="data-workspace label-workspace grid min-w-0 gap-3">
    {notice && <SuccessToast title={notice.title} error={notice.error} onClose={closeNotice}>{notice.message}</SuccessToast>}
    <section className="panel">
      <div className="awb-section-heading">
        <div><h3>Clearance Type</h3><p>{rows.length} data</p></div>
        <div className="flex flex-wrap items-center gap-3">
          <input ref={fileInput} type="file" accept=".xlsx,.xls" className="hidden" onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = "";
            if (file) void upload(file);
          }} />
          <a className="btn-ghost" href="/api/clearance-types/template">Download Template</a>
          <button type="button" className="btn-clay" disabled={busy} onClick={() => fileInput.current?.click()}>
            {busy ? "Mengimpor…" : "Import Excel"}
          </button>
        </div>
      </div>
    </section>
    <section className="panel">
      <form onSubmit={(event) => { event.preventDefault(); setAppliedFilters(filters); setPage(1); }}>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          {columns.map(([key, label]) => <label key={key} className="text-xs font-bold">{label}
            <input className="field mt-1" value={filters[key]}
              onChange={(event) => setFilters({ ...filters, [key]: event.target.value })} />
          </label>)}
        </div>
        <div className="flex flex-wrap gap-3 mt-4">
          <button className="btn-clay" type="submit">Terapkan Filter</button>
          <button className="btn-ghost" type="button" onClick={() => {
            setFilters(initialFilters); setAppliedFilters(initialFilters); setPage(1);
          }}>Reset</button>
        </div>
      </form>
    </section>
    <section className="panel min-w-0">
      <div className="awb-table-scroll" role="region" aria-label="Daftar Clearance Type" tabIndex={0}>
        <table className="awb-data-table w-full">
          <thead><tr>{columns.map(([key, label]) => <th scope="col" key={key}>{label}</th>)}</tr></thead>
          <tbody>
            {visibleRows.map((row) => <tr key={JSON.stringify([row.mawb, row.hawb])}>
              {columns.map(([key]) => <td key={key}>{row[key]}</td>)}
            </tr>)}
            {!visibleRows.length && <tr><td colSpan={columns.length}>{loading ? "Memuat data…" : "Tidak ada data."}</td></tr>}
          </tbody>
        </table>
      </div>
      {totalPages > 1 && <div className="flex flex-wrap items-center justify-end gap-3 mt-4">
        <button type="button" className="btn-ghost" disabled={currentPage === 1} onClick={() => setPage(currentPage - 1)}>Sebelumnya</button>
        <span>Halaman {currentPage} dari {totalPages}</span>
        <button type="button" className="btn-ghost" disabled={currentPage === totalPages} onClick={() => setPage(currentPage + 1)}>Berikutnya</button>
      </div>}
    </section>
  </div>;
}
