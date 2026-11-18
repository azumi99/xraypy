import { useCallback, useEffect, useRef, useState } from "react";
import api, { del } from "../api";
import { TrashIcon } from "../icons";
import { groupAwbs } from "../awbGroups";
import { useAwbDisplayStore } from "../store/awbDisplay";
import { shouldRefreshAwbs } from "../awbEvents";
import { groupManifestRequests } from "../manifestRequests";

const PAGE_SIZES = [10, 25, 50];
const EMPTY_FILTERS = { mawb: "", hawb: "", airlines: "", flight: "" };

export default function AwbTab({ onChanged, onOpenDetail, onEvent, connected }) {
  const [items, setItems] = useState([]);
  const [error, setError] = useState(null);
  const grouped = useAwbDisplayStore((s) => s.grouped);
  const setGrouped = useAwbDisplayStore((s) => s.setGrouped);
  const [attempt, setAttempt] = useState(0);
  // DataTables-style controls
  const [search, setSearch] = useState("");
  const [pageSize, setPageSize] = useState(10);
  const [page, setPage] = useState(1);
  const [manifests, setManifests] = useState({});
  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [applied, setApplied] = useState(EMPTY_FILTERS);
  const [loadAllManifests, setLoadAllManifests] = useState(false);

  const requestVersion = useRef(0);
  const refreshTimer = useRef(null);
  const load = useCallback(async () => {
    const version = ++requestVersion.current;
    try {
      const d = await api("/awb");
      if (version !== requestVersion.current) return;
      d.items.sort((x, y) => y.id - x.id); // terbaru di atas (desc)
      setItems(d.items);
      setError(null);
      setAttempt((n) => n + 1);
    } catch { if (version === requestVersion.current) setError("Daftar AWB tidak dapat dimuat. Data yang ditampilkan mungkin tidak terbaru."); }
  }, []);
  const scheduleLoad = useCallback(() => {
    clearTimeout(refreshTimer.current);
    refreshTimer.current = setTimeout(load, 100);
  }, [load]);

  useEffect(() => {
    scheduleLoad();
    const unsubscribe = onEvent((event) => { if (shouldRefreshAwbs(event)) scheduleLoad(); });
    return () => { clearTimeout(refreshTimer.current); requestVersion.current += 1; unsubscribe(); };
  }, [scheduleLoad, onEvent]);
  useEffect(() => { if (connected) scheduleLoad(); }, [connected, scheduleLoad]);

  const remove = async (id) => {
    try {
      try { await del(`/awb/${id}`); }
      catch (e) {
        if (!e.message.includes("409")) throw e;
        if (!confirm("AWB sudah terpakai (PAIRED). Hapus dengan force?\nPaket terkait akan menjadi TANPA AWB dan tercatat di audit log.")) return;
        await del(`/awb/${id}?force=true`);
      }
      await load();
      onChanged?.();
    } catch (e) {
      setError(`AWB tidak dapat dihapus: ${e.message}`);
    }
  };

  // DataTables: global search + length menu + paginate
  const q = search.trim().toLowerCase();
  const matches = (a) => [a.awb_number, a.hawb_number, a.part, a.total, a.status, a.status === "PAIRED" ? "complete" : "", a.parcel?.idx].some((v) => String(v ?? "").toLowerCase().includes(q));
  const manifestFilter = Boolean(applied.airlines.trim() || applied.flight.trim());
  const filteredItems = items.filter((item) => {
    const match = (value, query) => String(value ?? "").toLowerCase().includes(query.trim().toLowerCase());
    const manifest = manifests[item.id]?.data;
    return match(item.awb_number, applied.mawb) && match(item.hawb_number, applied.hawb)
      && match(manifest?.airlines_code, applied.airlines) && match(manifest?.flight_no, applied.flight);
  });
  const rows = grouped ? groupAwbs(filteredItems, manifests) : filteredItems;
  const filtered = !q ? rows : grouped
    ? rows.filter((group) => group.status.toLowerCase().includes(q) || group.items.some(matches))
    : rows.filter(matches);
  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
  const safePage = Math.min(page, totalPages);
  const pageItems = filtered.slice((safePage - 1) * pageSize, safePage * pageSize);
  const pageKey = JSON.stringify(groupManifestRequests(grouped || manifestFilter || loadAllManifests ? items : pageItems));
  useEffect(() => {
    let active = true;
    const queue = JSON.parse(pageKey);
    setManifests({});
    const worker = async () => {
      while (queue.length && active) {
        const ids = queue.shift();
        try {
          const result = await api(`/awb/${ids[0]}/manifest`);
          if (active) setManifests((previous) => ({ ...previous, ...Object.fromEntries(ids.map((id) => [id, result])) }));
        } catch {
          if (active) setManifests((previous) => ({ ...previous, ...Object.fromEntries(ids.map((id) => [id, { status: "unavailable" }])) }));
        }
      }
    };
    const concurrency = Math.min(3, queue.length);
    for (let i = 0; i < concurrency; i++) void worker();
    return () => { active = false; };
  }, [pageKey, attempt]);
  const firstEntry = filtered.length === 0 ? 0 : (safePage - 1) * pageSize + 1;
  const lastEntry = Math.min(safePage * pageSize, filtered.length);

  const goto = (n) => setPage(Math.min(Math.max(1, n), totalPages));
  const statusColor = { PAIRED: "text-ok", WAITING: "st-waiting", CANCELLED: "text-muted" };

  return (
    <div className="grid gap-3 w-full">
      <section className="panel awb-display-mode">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h3>Display Mode</h3>
          <div className="flex gap-2" aria-label="Display Mode">
            <button type="button" className={grouped ? "btn-clay" : "btn-ghost"} aria-pressed={grouped} onClick={() => { setGrouped(true); setPage(1); }}>Grouping</button>
            <button type="button" className={!grouped ? "btn-clay" : "btn-ghost"} aria-pressed={!grouped} onClick={() => { setGrouped(false); setPage(1); }}>Non-Grouping</button>
          </div>
        </div>
        <form onSubmit={(e) => { e.preventDefault(); setApplied(filters); setPage(1); }}>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 mt-4">
            {[["mawb", "MAWB"], ["hawb", "HAWB"], ["airlines", "Airlines"], ["flight", "Flight"]].map(([key, label]) => {
              const values = items.map((item) => key === "mawb" ? item.awb_number : key === "hawb" ? item.hawb_number : key === "airlines" ? manifests[item.id]?.data?.airlines_code : manifests[item.id]?.data?.flight_no);
              return <FilterSuggestion key={key} label={label} value={filters[key]} values={values} onFocus={() => { if (key === "airlines" || key === "flight") setLoadAllManifests(true); }} onChange={(value) => setFilters((current) => ({ ...current, [key]: value }))} />;
            })}
          </div>
          <div className="flex gap-3 mt-4"><button className="btn-clay" type="submit">Apply Filters</button><button className="btn-ghost" type="button" onClick={() => { setFilters(EMPTY_FILTERS); setApplied(EMPTY_FILTERS); setPage(1); }}>Reset</button></div>
        </form>
      </section>
    <div className="panel awb-table-panel w-full">
      <div className="awb-section-heading">
        <h3>Daftar AWB</h3>
      </div>
      {error && <p role="alert" className="text-bad text-[11.5px] my-2">{error} <button className="manifest-retry" onClick={load}>Coba lagi</button></p>}

      {/* DataTables top bar: length menu kiri, search kanan */}
      <div className="awb-toolbar flex items-center justify-between flex-wrap gap-3">
        <label className="muted flex items-center gap-2 text-xs">
          <select
            className="select-clay"
            aria-label="Jumlah baris per halaman"
            value={pageSize}
            onChange={(e) => { setPageSize(Number(e.target.value)); setPage(1); }}
          >
            {PAGE_SIZES.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>
      </div>

      <div className="awb-table-scroll">
      <table className="awb-data-table w-full">
        <thead>
          {grouped ? <tr><th>#</th><th>MAWB</th><th>Airlines</th><th>Origin</th><th>Pieces</th><th>Status</th></tr> :
          <tr><th>#</th><th>MAWB</th><th>HAWB</th><th>Airlines</th><th>Origin</th><th>Status</th><th>Date Time</th><th>Aksi</th></tr>}
        </thead>
        <tbody>
          {grouped ? pageItems.map((group, i) => <tr key={group.items[0].id} className="cursor-pointer" onClick={() => onOpenDetail?.(group.items[0].id)}>
            <td className="muted">{(safePage - 1) * pageSize + i + 1}</td>
            <td>{group.mawb || "—"}</td>
            <td title={group.airlines === null ? "Manifest tidak tersedia" : undefined}>{group.airlines ?? "…"}</td>
            <td title={group.origin === null ? "Manifest tidak tersedia" : undefined}>{group.origin ?? "…"}</td>
            <td><span className={`awb-status ${group.status === "INCOMPLETE" ? "st-incomplete" : group.status === "COMPLETE" ? "text-ok" : group.status === "LOADING" || group.status === "UNAVAILABLE" ? "st-waiting" : "text-bad"}`}>{group.scanned} / {group.status === "LOADING" ? "…" : group.expected ?? "tidak tersedia"}</span></td>
            <td><span className={`awb-status ${group.status === "COMPLETE" ? "text-ok" : group.status === "INCOMPLETE" ? "st-incomplete" : group.status === "OVER EXPECTED" ? "text-bad" : "st-waiting"}`}>{group.status}</span></td>
          </tr>) : pageItems.map((a, i) => {
            const canDelete = a.status !== "PAIRED";
            const title = canDelete ? "Hapus permanen" : "Hapus permanen (force — paket terkait jadi tanpa AWB, tercatat di audit)";
            return (
              <tr key={a.id} className="cursor-pointer" onClick={() => onOpenDetail?.(a.id)}>
                <td className="muted">{(safePage - 1) * pageSize + i + 1}</td>
                <td><button className="awb-number" onClick={(e) => { e.stopPropagation(); onOpenDetail?.(a.id); }}>{a.awb_number}</button></td>
                <td>{a.hawb_number ?? "—"}</td>
                <td title={manifests[a.id]?.status === "unavailable" ? "Manifest tidak tersedia" : undefined}>{manifests[a.id] ? (manifests[a.id].data?.airlines_code ?? "—") : "…"}</td>
                <td title={manifests[a.id]?.status === "unavailable" ? "Manifest tidak tersedia" : undefined}>{manifests[a.id] ? (manifests[a.id].data?.origin ?? "—") : "…"}</td>
                <td><span className={`awb-status ${statusColor[a.status] ?? ""}`}>{a.status === "PAIRED" ? "COMPLETE" : a.status}</span></td>
                <td>{a.created_at && !Number.isNaN(new Date(a.created_at).getTime()) ? new Date(a.created_at).toLocaleString("id-ID", { dateStyle: "medium", timeStyle: "short" }) : "—"}</td>
                <td>
                  <button
                    className="awb-row-delete"
                    title={title}
                    aria-label={`Hapus AWB ${a.awb_number}`}
                    onClick={(e) => { e.stopPropagation(); remove(a.id); }}
                  >
                    <TrashIcon /> Hapus
                  </button>
                </td>
              </tr>
            );
          })}
          {filtered.length === 0 && (
            <tr><td colSpan={grouped ? 6 : 8} className="muted text-center !py-10">Tidak ada data AWB yang cocok.</td></tr>
          )}
        </tbody>
      </table>
      </div>

      {/* DataTables bottom bar: info kiri, pagination kanan */}
      <div className="flex items-center justify-between flex-wrap gap-2 mt-3">
        <span className="muted text-[11.5px]">
          Menampilkan {firstEntry}–{lastEntry} dari {filtered.length} baris
          {q && ` (difilter dari ${rows.length})`}
        </span>
        <div className="flex items-center gap-1">
          <PageBtn label="First" disabled={safePage <= 1} onClick={() => goto(1)} />
          <PageBtn label="Prev" disabled={safePage <= 1} onClick={() => goto(safePage - 1)} />
          {Array.from({ length: totalPages }, (_, i) => i + 1).map((n) => (
            <button
              key={n}
              className={`min-w-8 h-8 px-2 rounded-lg font-bold text-[12px] cursor-pointer border-2 transition-colors duration-200 ${
                n === safePage
                  ? "bg-[var(--cta)] border-[var(--text)] text-white shadow-[3px_3px_0_var(--text)]"
                  : "bg-[var(--bg)] border-[var(--border)] text-muted hover:border-[var(--text)] hover:text-[var(--text)]"
              }`}
              onClick={() => goto(n)}
            >
              {n}
            </button>
          ))}
          <PageBtn label="Next" disabled={safePage >= totalPages} onClick={() => goto(safePage + 1)} />
          <PageBtn label="Last" disabled={safePage >= totalPages} onClick={() => goto(totalPages)} />
        </div>
      </div>
      <p className="muted text-[11.5px] mt-2">{grouped ? "Jumlah manifest dijumlahkan untuk setiap HAWB yang ada di antrean. Klik HAWB untuk detail dan foto X-ray." : "Klik baris untuk melihat detail + foto X-ray ukuran penuh."}</p>
    </div>
    </div>
  );
}

function FilterSuggestion({ label, value, values, onChange, onFocus }) {
  const [open, setOpen] = useState(false);
  const suggestions = [...new Set(values.filter(Boolean))].sort().filter((item) => String(item).toLowerCase().includes(value.trim().toLowerCase()));
  return <div className="awb-suggestion" onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false); }}>
    <label className="text-xs font-bold">{label}<input className="field mt-1" value={value} autoComplete="off" aria-expanded={open && suggestions.length > 0} onFocus={() => { setOpen(true); onFocus(); }} onChange={(event) => { onChange(event.target.value); setOpen(true); }} onKeyDown={(event) => { if (event.key === "Escape") setOpen(false); }} /></label>
    {open && suggestions.length > 0 && <div className="awb-suggestion-list" aria-label={`Saran ${label}`}>{suggestions.map((item) => <button type="button" key={item} onClick={() => { onChange(item); setOpen(false); }}>{item}</button>)}</div>}
  </div>;
}

function PageBtn({ label, disabled, onClick }) {
  return (
    <button
      className="px-2.5 h-8 rounded-lg font-bold text-[12px] cursor-pointer bg-[var(--bg)] border-2 border-[var(--border)] text-muted transition-colors duration-200 hover:border-[var(--text)] hover:text-[var(--text)] disabled:opacity-40 disabled:cursor-not-allowed"
      disabled={disabled}
      onClick={onClick}
    >
      {label}
    </button>
  );
}
