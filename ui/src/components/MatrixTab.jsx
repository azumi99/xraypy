import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import api from "../api";
import { shouldRefreshAwbs } from "../awbEvents";
import ExpandRowButton from "./ExpandRowButton";
import SuccessToast from "./SuccessToast";

const PAGE_SIZES = [10, 20, 50, 100];
const EMPTY_FILTERS = { mawb: "", hawb: "", from: "", to: "" };
const FILTER_FIELDS = [
  ["mawb", "MAWB"], ["hawb", "HAWB"],
  ["from", "Dari waktu foto X-ray (WIB)"], ["to", "Sampai waktu foto X-ray (WIB)"],
];
const dateTime = new Intl.DateTimeFormat("id-ID", {
  dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Jakarta",
});

function captureTimestamp(item) {
  const parcel = item.parcel;
  return parcel?.crop_ref || parcel?.crop_ref_side ? Date.parse(parcel.created_at) : NaN;
}

function orderedPieces(shipment) {
  return [...shipment.records].sort((a, b) =>
    (a.part ?? Infinity) - (b.part ?? Infinity) || a.id - b.id);
}

function formatTimeRange(first, last, expanded) {
  if (last === null) return "—";
  if (!expanded || first === last) return dateTime.format(last);
  return `${dateTime.format(first)} – ${dateTime.format(last)}`;
}

function groupByHawb(items) {
  const groups = new Map();
  for (const item of items) {
    const hawb = item.hawb_number?.trim();
    const key = hawb ? `hawb:${hawb}` : `awb:${item.id}`;
    if (!groups.has(key)) groups.set(key, { key, hawb, shipments: new Map(), firstCapture: null, capture: null });
    const group = groups.get(key);
    const mawb = item.awb_number?.trim();
    if (!group.shipments.has(mawb)) group.shipments.set(mawb, { id: item.id, total: null, parts: new Set(), records: [] });
    const shipment = group.shipments.get(mawb);
    shipment.records.push(item);
    if (item.part != null) shipment.parts.add(item.part);
    if (Number.isSafeInteger(item.total) && item.total > 0) {
      shipment.total = Math.max(shipment.total ?? 0, item.total);
    }
    const captured = captureTimestamp(item);
    if (Number.isFinite(captured)) {
      group.firstCapture = Math.min(group.firstCapture ?? captured, captured);
      group.capture = Math.max(group.capture ?? captured, captured);
    }
  }
  return [...groups.values()];
}

function manifestCreatedAt(manifest) {
  const value = manifest?.data?.created_at;
  if (manifest?.status !== "found" || typeof value !== "string") return null;
  const datetime = value.trim().replace(" ", "T");
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(datetime)) return null;
  const hasOffset = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(datetime);
  const timestamp = Date.parse(hasOffset ? datetime : `${datetime}+07:00`);
  return Number.isFinite(timestamp) ? timestamp : null;
}

async function loadManifest(id, signal) {
  return api(`/awb/${id}/manifest`, { signal, cache: "no-store" })
    .catch(() => ({ status: "unavailable" }));
}

function shipmentSummary(entries, manifests) {
  const results = entries.map(([, shipment]) => manifests[shipment.id]);
  const times = results.map(manifestCreatedAt).filter((time) => time !== null);
  const quantities = entries.map(([, shipment], index) => {
    const quantity = Number(results[index]?.data?.quantity);
    return shipment.total ?? (Number.isSafeInteger(quantity) && quantity > 0 ? quantity : shipment.parts.size || null);
  });
  const pieces = quantities.every((value) => value !== null) ? quantities.reduce((sum, value) => sum + value, 0) : null;
  return {
    results, quantities, pieces,
    firstManifestTime: times.length ? Math.min(...times) : null,
    manifestTime: times.length ? Math.max(...times) : null,
  };
}

export default function MatrixTab({ onEvent, connected }) {
  const [items, setItems] = useState(null);
  const [error, setError] = useState(false);
  const [revision, setRevision] = useState(0);
  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [applied, setApplied] = useState(EMPTY_FILTERS);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [manifests, setManifests] = useState({});
  const [expanded, setExpanded] = useState(null);
  const exportDialog = useRef(null);
  const exportRequest = useRef(null);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState("");
  const [exportNotice, setExportNotice] = useState(null);
  const closeExportNotice = useCallback(() => setExportNotice(null), []);
  useEffect(() => () => exportRequest.current?.abort(), []);

  useEffect(() => onEvent((event) => {
    if (shouldRefreshAwbs(event)) setRevision((value) => value + 1);
  }), [onEvent]);

  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const result = await api("/awb", { signal: controller.signal, cache: "no-store" });
        if (!controller.signal.aborted) {
          setItems([...result.items].sort((a, b) => b.id - a.id));
          setError(false);
        }
      } catch {
        if (!controller.signal.aborted) setError(true);
      }
    }, 100);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [revision, connected]);

  const groups = useMemo(() => groupByHawb(items ?? []), [items]);
  const mawbTerm = applied.mawb.trim().toLowerCase();
  const hawbTerm = applied.hawb.trim().toLowerCase();
  const fromTime = applied.from ? Date.parse(`${applied.from}+07:00`) : -Infinity;
  const toTime = applied.to ? Date.parse(`${applied.to}+07:00`) : Infinity;
  const invalidRange = Boolean(filters.from && filters.to && filters.from > filters.to);
  const filtered = groups.filter(({ hawb, shipments, capture }) =>
    (!mawbTerm || [...shipments.keys()].some((mawb) => mawb?.toLowerCase().includes(mawbTerm))) &&
    (hawb ?? "").toLowerCase().includes(hawbTerm) &&
    ((!applied.from && !applied.to) ||
      (capture !== null && capture >= fromTime && capture < toTime + 60_000)));
  const suggestions = useMemo(() => ({
    mawb: [...new Set(groups.flatMap(({ shipments }) => [...shipments.keys()]).filter(Boolean))].sort(),
    hawb: groups.map(({ hawb }) => hawb).filter(Boolean).sort(),
  }), [groups]);
  const pages = Math.max(1, Math.ceil(filtered.length / pageSize));
  const current = Math.min(page, pages);
  const visible = filtered.slice((current - 1) * pageSize, current * pageSize);
  const manifestKey = JSON.stringify(visible.flatMap(({ hawb, shipments }) =>
    hawb ? [...shipments].filter(([mawb]) => mawb).map(([, shipment]) => shipment.id) : []));

  useEffect(() => {
    const controller = new AbortController();
    const queue = JSON.parse(manifestKey);
    const lookup = async () => {
      while (queue.length && !controller.signal.aborted) {
        const id = queue.shift();
        const manifest = await loadManifest(id, controller.signal);
        if (!controller.signal.aborted) setManifests((previous) => ({ ...previous, [id]: manifest }));
      }
    };
    for (let i = Math.min(3, queue.length); i > 0; i--) void lookup();
    return () => controller.abort();
  }, [manifestKey, revision, connected]);

  const download = async (format) => {
    if (exportRequest.current) return;
    const controller = new AbortController();
    exportRequest.current = controller;
    setExporting(true);
    setExportError("");
    try {
      const results = { ...manifests };
      const queue = filtered.flatMap(({ hawb, shipments }) => hawb
        ? [...shipments].filter(([mawb, { id }]) => mawb && (!results[id] || results[id].status === "unavailable"))
          .map(([, { id }]) => id)
        : []);
      await Promise.all(Array.from({ length: Math.min(queue.length, 3) }, async () => {
        while (queue.length && !controller.signal.aborted) {
          const id = queue.shift();
          results[id] = await loadManifest(id, controller.signal);
        }
      }));
      if (controller.signal.aborted) return;
      const rows = filtered.flatMap(({ hawb, shipments }) =>
        [...shipments].flatMap(([mawb, shipment]) => {
          const manifestTime = manifestCreatedAt(results[shipment.id]);
          return orderedPieces(shipment).map((item) => {
            const captured = captureTimestamp(item);
            return {
              mawb: mawb || "",
              hawb: hawb || "",
              pieces: item.part ?? null,
              manifest_at: manifestTime === null ? "" : dateTime.format(manifestTime),
              xray_at: Number.isFinite(captured) ? dateTime.format(captured) : "",
            };
          });
        }));
      const response = await fetch("/api/awb/matrix/export", {
        method: "POST", signal: controller.signal,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ format, rows }),
      });
      if (!response.ok) throw new Error("Export gagal");
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = `matrix.${format}`;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      exportDialog.current?.close();
      setExportNotice({ format, count: rows.length });
    } catch {
      if (!controller.signal.aborted) setExportError("File gagal dibuat. Coba lagi.");
    } finally {
      exportRequest.current = null;
      if (!controller.signal.aborted) setExporting(false);
    }
  };

  return (
    <div className="data-workspace label-workspace grid min-w-0 grid-cols-[minmax(0,1fr)] gap-3">
      <section className="panel" aria-label="Filter Matrix">
        <form onSubmit={(event) => {
          event.preventDefault();
          if (invalidRange) return;
          setApplied(filters);
          setPage(1);
        }}>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
            {FILTER_FIELDS.map(([key, label]) => {
              const options = suggestions[key];
              return <label key={key} className="text-xs font-bold min-w-0">
                {label}
                <input className="field mt-1" type={options ? "text" : "datetime-local"}
                  list={options ? `matrix-filter-${key}` : undefined} value={filters[key]}
                  aria-invalid={!options && invalidRange ? true : undefined}
                  onChange={(event) => setFilters({ ...filters, [key]: event.target.value })} />
                {options && <datalist id={`matrix-filter-${key}`}>
                  {options.map((value) => <option key={value} value={value} />)}
                </datalist>}
              </label>;
            })}
          </div>
          {invalidRange && <p role="alert" className="text-bad text-sm mt-3">
            Waktu akhir harus sama dengan atau setelah waktu mulai.
          </p>}
          <div className="flex flex-wrap gap-3 mt-4">
            <button className="btn-clay" type="submit" disabled={invalidRange}>Apply Filters</button>
            <button className="btn-ghost" type="button" onClick={() => {
              setFilters(EMPTY_FILTERS);
              setApplied(EMPTY_FILTERS);
              setPage(1);
            }}>Reset</button>
          </div>
        </form>
      </section>
      <section className="panel awb-table-panel">
        {error && <p role="alert" className="text-bad mb-3">Data Matrix gagal dimuat.{" "}
          <button type="button" className="btn-ghost" onClick={() => setRevision((value) => value + 1)}>Coba lagi</button>
        </p>}
        <div className="awb-toolbar flex items-center justify-between flex-wrap gap-3">
          <select className="select-clay" aria-label="Jumlah baris per halaman" value={pageSize}
            onChange={(event) => {
              setPageSize(Number(event.target.value));
              setPage(1);
            }}>
            {PAGE_SIZES.map((size) => <option key={size} value={size}>{size}</option>)}
          </select>
          <button type="button" className="btn-clay" disabled={!filtered.length} onClick={() => {
            setExportError("");
            setExportNotice(null);
            exportDialog.current?.showModal();
          }}>Export</button>
        </div>
        <div className="awb-table-scroll" tabIndex={0} role="region" aria-label="Tabel Matrix, dapat digeser horizontal">
          <table className="awb-data-table w-full">
            <thead><tr><th scope="col" aria-label="Buka atau tutup rincian" className="w-12"></th><th scope="col">MAWB</th><th scope="col">HAWB</th><th scope="col">Pieces</th>
              <th scope="col">Waktu dibuat di manifest (WIB)</th><th scope="col">Waktu foto X-ray terakhir (WIB)</th>
              <th scope="col">Waktu kirim CEISA (WIB)</th></tr></thead>
            <tbody>
              {!visible.length && <tr><td colSpan={7} className="muted text-center !py-8">
                {error ? "Data tidak tersedia." : items === null ? "Memuat data…" : "Tidak ada AWB yang cocok."}
              </td></tr>}
              {visible.map(({ key, hawb, shipments, firstCapture, capture }) => {
                const entries = [...shipments];
                const { results, quantities, pieces, firstManifestTime, manifestTime } = shipmentSummary(entries, manifests);
                const pending = hawb && entries.some(([mawb], index) => mawb && !results[index]);
                const isExpanded = expanded === key;
                const toggleDetails = () => setExpanded(isExpanded ? null : key);
                return <Fragment key={key}>
                  <tr className={isExpanded ? "cursor-pointer font-bold" : "cursor-pointer"} onClick={toggleDetails}>
                    <td><ExpandRowButton expanded={isExpanded}
                      label={`Rincian Pieces ${hawb || entries[0][0] || ""}`} onToggle={toggleDetails} /></td>
                    <td>{[...shipments.keys()].filter(Boolean).join(", ") || "—"}</td>
                    <td>{hawb || "—"}</td>
                    <td>{pieces ?? "—"}</td>
                    <td>{manifestTime === null && pending ? "Memuat…" : formatTimeRange(firstManifestTime, manifestTime, isExpanded)}</td>
                    <td>{formatTimeRange(firstCapture, capture, isExpanded)}</td>
                    <td>—</td>
                  </tr>
                  {isExpanded && entries.flatMap(([mawb, shipment], index) => {
                    const manifestTime = manifestCreatedAt(results[index]);
                    const records = orderedPieces(shipment);
                    const missing = (quantities[index] ?? 0) - shipment.parts.size;
                    return [
                      ...records.map((item) => {
                        const captured = captureTimestamp(item);
                        return <tr key={item.id} className="bg-[var(--bg)]">
                          <td></td>
                          <td className="muted">{mawb || "—"}</td>
                          <td className="muted">{hawb || "—"}</td>
                          <td>Part {item.part ?? "—"} / {quantities[index] ?? "—"}</td>
                          <td>{manifestTime === null ? "—" : dateTime.format(manifestTime)}</td>
                          <td>{Number.isFinite(captured) ? dateTime.format(captured) : "—"}</td>
                          <td>—</td>
                        </tr>;
                      }),
                      missing > 0 && <tr key={`missing-${shipment.id}`} className="bg-[var(--bg)]">
                        <td colSpan={7} className="muted">{missing} part belum tercatat untuk MAWB {mawb || "—"}.</td>
                      </tr>,
                    ];
                  })}
                </Fragment>;
              })}
            </tbody>
          </table>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-3 mt-4 text-xs muted">
          <div className="flex items-center gap-3">
            <button type="button" className="btn-ghost !py-1 !px-3" disabled={current <= 1}
              onClick={() => setPage(current - 1)}>Prev</button>
            <span>{current} / {pages}</span>
            <button type="button" className="btn-ghost !py-1 !px-3" disabled={current >= pages}
              onClick={() => setPage(current + 1)}>Next</button>
          </div>
        </div>
      </section>
      {exportNotice && <SuccessToast title="Export Matrix berhasil" onClose={closeExportNotice}>
        {exportNotice.count} baris piece diekspor ke {exportNotice.format.toUpperCase()}.
      </SuccessToast>}
      <dialog ref={exportDialog} aria-labelledby="matrix-export-title"
        className="panel m-auto w-96 max-w-[calc(100%_-_2rem)] text-[var(--text)] backdrop:bg-black/50"
        onCancel={(event) => { if (exporting) event.preventDefault(); }}>
        <h3 id="matrix-export-title">Export Matrix</h3>
        <p className="muted text-sm mt-2">{filtered.reduce((sum, group) => sum + [...group.shipments.values()]
          .reduce((count, shipment) => count + shipment.records.length, 0), 0)} baris piece sesuai filter aktif.</p>
        {exportError && <p role="alert" className="text-bad mt-3">{exportError}</p>}
        {exporting && <p role="status" className="muted mt-3">Menyiapkan file…</p>}
        <div className="flex flex-wrap gap-3 mt-4">
          <button type="button" className="btn-clay" disabled={exporting} onClick={() => void download("xlsx")}>XLSX</button>
          <button type="button" className="btn-ghost" disabled={exporting} onClick={() => void download("csv")}>CSV</button>
          <button type="button" className="btn-ghost" disabled={exporting} onClick={() => exportDialog.current?.close()}>Batal</button>
        </div>
      </dialog>
    </div>
  );
}
