import { useCallback, useEffect, useState } from "react";
import { Area, CartesianGrid, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import api from "../api";
import { shouldRefreshAwbs } from "../awbEvents";
import { groupAwbs } from "../awbGroups";
import { groupManifestRequests } from "../manifestRequests";

export default function Dashboard({ session, onEvent }) {
  const [items, setItems] = useState(null);
  const [error, setError] = useState(false);
  const load = useCallback(async () => {
    try {
      const data = await api("/awb");
      setItems(data.items);
      setError(false);
    } catch {
      setError(true);
    }
  }, []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => onEvent((event) => {
    if (shouldRefreshAwbs(event)) load();
  }), [onEvent, load]);

  const [manifestSnapshot, setManifestSnapshot] = useState(null);
  useEffect(() => {
    if (!items) return;
    const controller = new AbortController();
    const queue = groupManifestRequests(items.filter((item) => item.hawb_number?.trim()));
    const manifests = {};
    async function loadManifests() {
      while (queue.length && !controller.signal.aborted) {
        const ids = queue.shift();
        const result = await api(`/awb/${ids[0]}/manifest`, { signal: controller.signal })
          .catch(() => ({ status: "unavailable" }));
        for (const id of ids) manifests[id] = result;
      }
    }
    Promise.all(Array.from({ length: Math.min(3, queue.length) }, loadManifests)).then(() => {
      if (!controller.signal.aborted) setManifestSnapshot({ items, manifests });
    });
    return () => controller.abort();
  }, [items]);

  const [batches, setBatches] = useState(null);
  const [batchError, setBatchError] = useState(false);
  useEffect(() => {
    let request;
    async function loadBatches() {
      request?.abort();
      const controller = new AbortController();
      request = controller;
      try {
        const data = await api("/session/batches", { signal: controller.signal, cache: "no-store" });
        if (controller.signal.aborted) return;
        setBatches(data.items);
        setBatchError(false);
      } catch {
        if (!controller.signal.aborted) setBatchError(true);
      }
    }
    loadBatches();
    const unsubscribe = onEvent((event) => {
      if (event.type?.startsWith("parcel.") || event.type === "session.stopped") loadBatches();
    });
    return () => {
      request?.abort();
      unsubscribe();
    };
  }, [onEvent]);

  const batchTimestamp = (batchId) => {
    const timestamp = batchId.match(/(?:^|\/)(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})-/);
    if (!timestamp) return null;
    const [, year, month, day, hour, minute, second] = timestamp;
    return new Date(Date.UTC(+year, +month - 1, +day, +hour, +minute, +second));
  };
  const dateFormatter = new Intl.DateTimeFormat("id-ID", {
    timeZone: "Asia/Jakarta", dateStyle: "long",
  });
  const batchDate = (batchId) => {
    const timestamp = batchTimestamp(batchId);
    return timestamp && dateFormatter.format(timestamp);
  };
  const today = dateFormatter.format(new Date());
  const batchRows = batches ?? [];
  const todayBatches = batchRows.filter((batch) => batchDate(batch.batch_id) === today);
  const latestDate = batchRows.map((batch) => batchDate(batch.batch_id)).find(Boolean);
  const visibleBatches = todayBatches.length ? todayBatches : batchRows.filter(
    (batch) => latestDate ? batchDate(batch.batch_id) === latestDate : true,
  );
  const batchPeriod = todayBatches.length ? "Hari ini" : latestDate && `Data terakhir · ${latestDate}`;

  const latestBatch = visibleBatches[0];
  const totalXrays = visibleBatches.reduce((total, batch) => total + batch.photo_count, 0);
  const latestTimestamp = latestBatch && batchTimestamp(latestBatch.batch_id);
  const latestTime = latestTimestamp && new Intl.DateTimeFormat("id-ID", {
    timeZone: "Asia/Jakarta", dateStyle: "long", timeStyle: "short",
  }).format(latestTimestamp);

  const count = (status) => items == null ? "—" : items.filter((item) => item.status === status).length;
  const groups = items && manifestSnapshot?.items === items
    ? groupAwbs(items, manifestSnapshot.manifests) : null;
  const incomplete = groups?.filter((group) => group.status === "INCOMPLETE").length ?? "—";
  const unavailableGroups = groups?.filter((group) => group.status === "UNAVAILABLE").length ?? 0;
  const cards = [
    ["Total AWB", items?.length ?? "—", "Semua nomor AWB", "total"],
    ["Waiting", count("WAITING"), "Dalam antrean", "waiting"],
    ["Complete", count("PAIRED"), "AWB dengan parcel", "paired"],
    ["Incomplete", incomplete, unavailableGroups
      ? `MAWB belum lengkap · ${unavailableGroups} tanpa data manifest`
      : "MAWB dengan jumlah parcel kurang", "incomplete"],
  ];
  const shortDate = new Intl.DateTimeFormat("id-ID", {
    timeZone: "Asia/Jakarta", day: "numeric", month: "short",
  });
  const now = Date.now();
  const daily = Array.from({ length: 7 }, (_, i) => {
    const date = new Date(now - (6 - i) * 86_400_000);
    return {
      date: dateFormatter.format(date), label: shortDate.format(date), WAITING: 0, PAIRED: 0,
      INCOMPLETE: groups ? 0 : null, batchCount: 0, xrayCount: 0,
    };
  });
  const days = new Map(daily.map((day) => [day.date, day]));
  for (const item of items ?? []) {
    const timestamp = item.created_at ? Date.parse(item.created_at) : NaN;
    if (!Number.isFinite(timestamp)) continue;
    const day = days.get(dateFormatter.format(timestamp));
    if (day && ["WAITING", "PAIRED"].includes(item.status)) day[item.status] += 1;
  }
  for (const group of groups ?? []) {
    if (group.status !== "INCOMPLETE") continue;
    const dates = group.items.map((item) => item.created_at ? Date.parse(item.created_at) : NaN)
      .filter(Number.isFinite);
    if (!dates.length) continue;
    const day = days.get(dateFormatter.format(Math.min(...dates)));
    if (day) day.INCOMPLETE += 1;
  }
  for (const batch of batchRows) {
    const day = days.get(batchDate(batch.batch_id));
    if (day) {
      day.batchCount += 1;
      day.xrayCount += batch.photo_count;
    }
  }

  return <div className="dashboard">
    {error && <div role="alert" className="dashboard-error">Data AWB tidak dapat dimuat. <button onClick={load}>Coba lagi</button></div>}
    <section className="dashboard-cards" aria-label="Statistik AWB">
      {cards.map(([label, value, description, type]) => <div key={type} className={`dashboard-card dashboard-card-${type}`}>
        <span className="dashboard-card-label">{label}</span><strong>{value}</strong><span className="dashboard-card-description">{description}</span>
      </div>)}
    </section>
    <div className="dashboard-content">
      <section className="dashboard-panel">
        <div className="dashboard-panel-heading"><div><span className="awb-eyebrow">STATISTIK</span><h3>AWB tujuh hari terakhir</h3><p className="muted text-xs">Waiting dan Complete per AWB. Incomplete per MAWB, berdasarkan tanggal AWB pertamanya.</p></div></div>
        {items == null ? <p className="dashboard-empty">{error ? "Data tidak tersedia." : "Memuat data…"}</p> : <>
          <div className="dashboard-area-chart" aria-label="Grafik AWB, batch, dan X-ray tujuh hari terakhir">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={daily} margin={{ top: 12, right: 4, left: -24, bottom: 0 }} accessibilityLayer>
                <defs>
                  <linearGradient id="awb-waiting" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#ed978d" stopOpacity={0.65} /><stop offset="100%" stopColor="#ed978d" stopOpacity={0.08} /></linearGradient>
                  <linearGradient id="awb-paired" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#22a65a" stopOpacity={0.65} /><stop offset="100%" stopColor="#22a65a" stopOpacity={0.08} /></linearGradient>
                </defs>
                <CartesianGrid vertical={false} stroke="var(--border)" strokeDasharray="3 3" />
                <XAxis dataKey="label" tickLine={false} axisLine={false} tick={{ fill: "var(--text-muted)", fontSize: 11 }} tickMargin={10} />
                <YAxis allowDecimals={false} tickLine={false} axisLine={false} tick={{ fill: "var(--text-muted)", fontSize: 11 }} />
                {batches !== null && !batchError && <YAxis yAxisId="capture" orientation="right" width={40} allowDecimals={false} tickLine={false} axisLine={false} tick={{ fill: "var(--text-muted)", fontSize: 11 }} />}
                <Tooltip contentStyle={{ background: "var(--bg)", border: "1px solid var(--border)", borderRadius: 10, color: "var(--text)", fontSize: 12 }} />
                <Area type="monotone" dataKey="WAITING" name="Waiting" stackId="1" stroke="#ed978d" fill="url(#awb-waiting)" />
                <Area type="monotone" dataKey="PAIRED" name="Complete" stackId="1" stroke="#22a65a" fill="url(#awb-paired)" />
                <Area type="monotone" dataKey="INCOMPLETE" name="Incomplete (MAWB)" stroke="#b577d1" fill="none" />
                {batches !== null && !batchError && <>
                  <Line yAxisId="capture" type="monotone" dataKey="batchCount" name="Batch" stroke="#579be5" strokeWidth={2} />
                  <Line yAxisId="capture" type="monotone" dataKey="xrayCount" name="X-ray" stroke="#dcaa43" strokeWidth={2} />
                </>}
              </ComposedChart>
            </ResponsiveContainer>
          </div>
          <div className="dashboard-chart-legend flex-wrap">
            <span><i style={{ background: "#ed978d" }} />Waiting</span>
            <span><i style={{ background: "#22a65a" }} />Complete</span>
            <span><i style={{ background: "#b577d1" }} />Incomplete (MAWB)</span>
            {batches !== null && !batchError && <>
              <span><i style={{ background: "#579be5" }} />Batch</span>
              <span><i style={{ background: "#dcaa43" }} />X-ray</span>
            </>}
          </div>
          {batches !== null && !batchError && <p className="muted text-xs">Skala kanan untuk batch dan X-ray.</p>}
        </>}
        {batchError && <p className="dashboard-empty">Data batch dan X-ray tidak tersedia.</p>}
      </section>
      <section className="dashboard-panel dashboard-session-panel"><span className="awb-eyebrow">PEMINDAIAN</span><h3>Sesi saat ini</h3><div className="dashboard-session-count">{session?.parcel_count ?? "—"}</div><p>Parcel diproses pada sesi ini</p><div className="dashboard-session-detail"><span>Batch</span><strong>{session?.batch_id ?? "Belum ada sesi"}</strong></div><div className="dashboard-session-detail"><span>Mode</span><strong>{session?.mode ?? "—"}</strong></div>
        <h4 className="mt-5 font-bold">{batches === null ? "Batch pemindaian" : `${visibleBatches.length} batch · ${totalXrays} X-ray`}</h4>
        {batchPeriod && <p className="muted text-xs">{batchPeriod}</p>}
        {batchError && <p role="alert" className="dashboard-empty">Jumlah X-ray per batch tidak dapat dimuat.</p>}
        {!batchError && batches === null && <p className="dashboard-empty">Memuat batch…</p>}
        {!batchError && batches !== null && visibleBatches.length === 0 && <p className="dashboard-empty">Belum ada batch dengan X-ray.</p>}
        {latestBatch && <div className="dashboard-session-detail">
          <span>Batch terakhir</span>
          <strong>{latestTime ? `${latestTime} WIB` : latestBatch.batch_id} · {latestBatch.photo_count} X-ray</strong>
        </div>}
      </section>
    </div>
  </div>;
}
