import { useEffect, useRef, useState } from "react";
import XrayMachineSettings from "./XrayMachineSettings";

const labels = {
  connecting: "Menghubungkan",
  connected: "Terhubung",
  stopped: "Berhenti",
  error: "Error",
  disabled: "Dinonaktifkan",
};

function statusLabel(tunnel) {
  if (!tunnel || typeof tunnel.enabled !== "boolean") return "Tidak diketahui";
  if (tunnel.enabled === false) return labels.disabled;
  return labels[tunnel.state] || "Tidak diketahui";
}

function StatusPill({ label, tone }) {
  return <span role="status" className={`settings-status settings-status-${tone}`}>
    <i aria-hidden="true" />{label}
  </span>;
}

function tunnelTone(tunnel) {
  if (!tunnel?.enabled) return "neutral";
  if (tunnel.state === "connected") return "good";
  if (tunnel.state === "error") return "bad";
  if (tunnel.state === "connecting") return "warn";
  return "neutral";
}

export default function SettingsTab({ warehouse }) {
  const [status, setStatus] = useState(null);
  const [sync, setSync] = useState(null);
  const [error, setError] = useState("");
  const [pending, setPending] = useState("");
  const busy = useRef(false);
  const mounted = useRef(false);

  useEffect(() => {
    mounted.current = true;
    let timer;
    async function refresh() {
      if (!busy.current) {
        busy.current = true;
        try {
          const response = await fetch("/api/settings/tunnels", { credentials: "same-origin", cache: "no-store" });
          if (!response.ok) throw new Error();
          const data = await response.json();
          if (data.warehouse !== warehouse || !data.manifest || !data.database) throw new Error();
          if (mounted.current) { setStatus(data); setError(""); }
          try {
            const syncResponse = await fetch("/api/settings/sync", { credentials: "same-origin", cache: "no-store" });
            if (syncResponse.ok && mounted.current) setSync(await syncResponse.json());
          } catch { if (mounted.current) setSync(null); }
        } catch {
          if (mounted.current) { setStatus(null); setError("Status tunnel tidak tersedia. Coba lagi nanti."); }
        } finally { busy.current = false; }
      }
      if (mounted.current) timer = setTimeout(refresh, 5000);
    }
    refresh();
    return () => { mounted.current = false; clearTimeout(timer); };
  }, [warehouse]);

  async function control(action) {
    if (busy.current || pending) return;
    if (action !== "start" && !window.confirm("Tindakan ini dapat mengganggu scan yang sedang berjalan. Lanjutkan?")) return;
    busy.current = true;
    setPending(action);
    setError("");
    try {
      const response = await fetch(`/api/settings/tunnels/manifest/${action}`, { method: "POST", credentials: "same-origin" });
      if (!response.ok) throw new Error();
      // Refresh independently of the command response so only current status is shown.
      const refreshed = await fetch("/api/settings/tunnels", { credentials: "same-origin", cache: "no-store" });
      if (!refreshed.ok) throw new Error();
      const data = await refreshed.json();
      if (data.warehouse !== warehouse || !data.manifest || !data.database) throw new Error();
      if (mounted.current) setStatus(data);
    } catch {
      if (mounted.current) { setStatus(null); setError("Tindakan atau pembaruan status gagal. Periksa sesi scan dan coba lagi."); }
    } finally {
      busy.current = false;
      if (mounted.current) setPending("");
    }
  }

  const manifest = status?.manifest;
  const warehouseSync = sync?.items?.[warehouse];
  const database = status?.database;
  const syncMetrics = warehouseSync ? [
    ["Tersinkron", warehouseSync.synced, "good"],
    ["Menunggu", warehouseSync.pending, "warn"],
    ["Gagal", warehouseSync.failed, "bad"],
    ["Belum terdaftar", warehouseSync.unknown, "neutral"],
  ] : [];
  const photoTotal = warehouseSync?.total ?? 0;

  return (
    <div className="settings-page">
      <header className="settings-heading">
        <p>Warehouse <strong>{warehouse.toUpperCase()}</strong></p>
        <span className="settings-refresh">Diperbarui setiap 5 detik</span>
      </header>
      {error && <p role="alert" className="dashboard-error">{error}</p>}
      <XrayMachineSettings key={warehouse} />
      <div className="settings-connections">
        <section className="dashboard-panel settings-panel" aria-labelledby="manifest-tunnel-title">
          <header className="settings-panel-heading">
            <div><h3 id="manifest-tunnel-title">Tunnel manifest</h3><p>Akses database manifest warehouse ini.</p></div>
            <StatusPill label={statusLabel(manifest)} tone={tunnelTone(manifest)} />
          </header>
          <div className="settings-actions" aria-label="Kontrol tunnel manifest">
            {[["start", "Start"], ["stop", "Stop"], ["restart", "Restart"]].map(([action, label]) => (
              <button key={action} type="button" className={action === "start" ? "btn-clay" : "btn-ghost"}
                disabled={manifest?.enabled !== true || !["connected", "stopped", "error"].includes(manifest?.state) || !!pending ||
                  (action === "start" && manifest?.state === "connected") || (action === "stop" && manifest?.state === "stopped")}
                onClick={() => control(action)}>
                {pending === action ? "Memproses…" : label}
              </button>
            ))}
          </div>
        </section>
        <section className="dashboard-panel settings-panel" aria-labelledby="database-tunnel-title">
          <header className="settings-panel-heading">
            <div><h3 id="database-tunnel-title">Tunnel PostgreSQL</h3><p>Database aplikasi yang dipakai bersama oleh seluruh warehouse.</p></div>
            <StatusPill label={statusLabel(database)} tone={tunnelTone(database)} />
          </header>
        </section>
      </div>
      <section className="dashboard-panel settings-panel" aria-labelledby="sync-tunnel-title">
        <header className="settings-panel-heading">
          <div><h3 id="sync-tunnel-title">Sinkronisasi Foto X-ray</h3><p>Pengiriman foto lokal ke server.</p></div>
          <StatusPill label={!sync ? "Tidak diketahui" : sync.enabled ? "Aktif" : "Nonaktif"} tone={sync?.enabled ? "good" : "neutral"} />
        </header>
        {sync?.enabled && warehouseSync && (
          <>
            <div className="settings-sync-summary">
              <div><strong>{photoTotal.toLocaleString("id-ID")}</strong> <span>foto terdata</span></div>
              {photoTotal > 0 && <span>{Math.round(100 * warehouseSync.synced / photoTotal)}% tersinkron</span>}
            </div>
            {photoTotal > 0 && <div className="settings-sync-bar" aria-hidden="true">
              {syncMetrics.map(([label, count, tone]) => (
                <span key={label} className={`settings-segment-${tone}`} style={{ flexGrow: count, flexBasis: 0 }} />
              ))}
            </div>}
            <dl className="settings-metrics">
              {syncMetrics.map(([label, count, tone]) => (
                <div key={label} className={`settings-metric settings-status-${tone}`}>
                  <dt>{label}</dt>
                  <dd>{count.toLocaleString("id-ID")}</dd>
                </div>
              ))}
            </dl>
          </>
        )}
      </section>
    </div>
  );
}
