import { useCallback, useEffect, useState } from "react";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import api from "../api";

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
    if (event.type?.startsWith("awb.") || event.type === "parcel.saved") load();
  }), [onEvent, load]);

  const count = (status) => items == null ? "—" : items.filter((item) => item.status === status).length;
  const cards = [
    ["Total AWB", items?.length ?? "—", "Semua nomor AWB", "total"],
    ["Waiting", count("WAITING"), "Dalam antrean", "waiting"],
    ["Complete", count("PAIRED"), "AWB dengan parcel", "paired"],
  ];
  const keyForDate = (date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  const daily = Array.from({ length: 7 }, (_, i) => {
    const date = new Date();
    date.setHours(0, 0, 0, 0);
    date.setDate(date.getDate() - (6 - i));
    return { date: keyForDate(date), label: date.toLocaleDateString("id-ID", { day: "numeric", month: "short" }), WAITING: 0, PAIRED: 0, CANCELLED: 0 };
  });
  for (const item of items ?? []) {
    if (!item.created_at) continue;
    const date = new Date(item.created_at);
    if (Number.isNaN(date.getTime())) continue;
    const day = daily.find((entry) => entry.date === keyForDate(date));
    if (day && ["WAITING", "PAIRED", "CANCELLED"].includes(item.status)) day[item.status] += 1;
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
        <div className="dashboard-panel-heading"><div><span className="awb-eyebrow">STATISTIK</span><h3>AWB tujuh hari terakhir</h3><p className="muted text-xs">Berdasarkan tanggal dibuat dan status saat ini</p></div></div>
        {items == null ? <p className="dashboard-empty">{error ? "Data tidak tersedia." : "Memuat data…"}</p> : <>
          <div className="dashboard-area-chart" aria-label="Grafik area AWB tujuh hari terakhir berdasarkan tanggal dibuat dan status saat ini">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={daily} margin={{ top: 12, right: 12, left: -24, bottom: 0 }} accessibilityLayer>
                <defs>
                  <linearGradient id="awb-waiting" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#ed978d" stopOpacity={0.65} /><stop offset="100%" stopColor="#ed978d" stopOpacity={0.08} /></linearGradient>
                  <linearGradient id="awb-paired" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#22a65a" stopOpacity={0.65} /><stop offset="100%" stopColor="#22a65a" stopOpacity={0.08} /></linearGradient>
                </defs>
                <CartesianGrid vertical={false} stroke="var(--border)" strokeDasharray="3 3" />
                <XAxis dataKey="label" tickLine={false} axisLine={false} tick={{ fill: "var(--text-muted)", fontSize: 11 }} tickMargin={10} />
                <YAxis allowDecimals={false} tickLine={false} axisLine={false} tick={{ fill: "var(--text-muted)", fontSize: 11 }} />
                <Tooltip contentStyle={{ background: "var(--bg)", border: "1px solid var(--border)", borderRadius: 10, color: "var(--text)", fontSize: 12 }} />
                <Area type="monotone" dataKey="WAITING" name="Waiting" stackId="1" stroke="#ed978d" fill="url(#awb-waiting)" />
                <Area type="monotone" dataKey="PAIRED" name="Complete" stackId="1" stroke="#22a65a" fill="url(#awb-paired)" />
              </AreaChart>
            </ResponsiveContainer>
          </div>
          <div className="dashboard-chart-legend"><span><i style={{ background: "#ed978d" }} />Waiting</span><span><i style={{ background: "#22a65a" }} />Complete</span></div>
        </>}
      </section>
      <section className="dashboard-panel dashboard-session-panel"><span className="awb-eyebrow">PEMINDAIAN</span><h3>Sesi saat ini</h3><div className="dashboard-session-count">{session?.parcel_count ?? "—"}</div><p>Parcel diproses pada sesi ini</p><div className="dashboard-session-detail"><span>Batch</span><strong>{session?.batch_id ?? "Belum ada sesi"}</strong></div><div className="dashboard-session-detail"><span>Mode</span><strong>{session?.mode ?? "—"}</strong></div></section>
    </div>
  </div>;
}
