import { useEffect, useRef, useState } from "react";

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
  return (
    <div className="max-w-4xl space-y-4">
      <p className="text-sm text-muted">Status koneksi diperbarui setiap 5 detik. Kontrol manifest hanya berlaku untuk warehouse Anda.</p>
      {error && <p role="alert" className="text-sm text-red-400">{error}</p>}
      <section className="panel" aria-labelledby="manifest-tunnel-title">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div><h3 id="manifest-tunnel-title">Tunnel database manifest</h3><p className="text-sm text-muted">Warehouse {warehouse.toUpperCase()}</p></div>
          <strong role="status" className="rounded-full border border-[var(--border)] bg-[var(--bg-cream)] px-3 py-1 text-sm">{statusLabel(manifest)}</strong>
        </div>
        <div className="mt-5 flex flex-wrap gap-3">
          {[["start", "Start"], ["stop", "Stop"], ["restart", "Restart"]].map(([action, label]) => (
            <button key={action} type="button" className={action === "start" ? "btn-clay disabled:opacity-50" : "btn-ghost disabled:opacity-50"} disabled={manifest?.enabled !== true || !["connected", "stopped", "error"].includes(manifest?.state) || !!pending || (action === "start" && manifest?.state === "connected") || (action === "stop" && manifest?.state === "stopped")} onClick={() => control(action)}>
              {pending === action ? "Memproses..." : label}
            </button>
          ))}
        </div>
      </section>
      <section className="panel" aria-labelledby="sync-tunnel-title">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div><h3 id="sync-tunnel-title">Sinkronisasi Foto X-ray</h3><p className="text-sm text-muted">Foto lokal dikirim otomatis ke server per warehouse.</p></div>
          <strong role="status" className="rounded-full border border-[var(--border)] bg-[var(--bg-cream)] px-3 py-1 text-sm">{!sync ? "Tidak diketahui" : sync.enabled ? "Aktif" : "Nonaktif"}</strong>
        </div>
        {sync?.enabled && (
          <div className="mt-5 grid gap-4 sm:grid-cols-2">
            {Object.entries(sync.items ?? {}).map(([site, item]) => (
              <div key={site} className="rounded-xl border border-[var(--border)] bg-[var(--bg-cream)] p-4">
                <div className="flex items-center justify-between">
                  <strong>{site.toUpperCase()}</strong>
                  <span className="muted text-xs">{item.total} foto</span>
                </div>
                <div className="mt-3 flex flex-wrap gap-3 text-xs">
                  <span className="text-ok font-bold">✓ {item.synced} tersinkron</span>
                  <span className="st-waiting font-bold">⏳ {item.pending} menunggu</span>
                  <span className="text-bad font-bold">✗ {item.failed} gagal</span>
                  <span className="muted font-bold">? {item.unknown} belum terdaftar</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
      <section className="panel" aria-labelledby="database-tunnel-title">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div><h3 id="database-tunnel-title">Tunnel PostgreSQL aplikasi</h3><p className="text-sm text-muted">Digunakan bersama oleh seluruh warehouse. Status saja.</p></div>
          <strong role="status" className="rounded-full border border-[var(--border)] bg-[var(--bg-cream)] px-3 py-1 text-sm">{statusLabel(status?.database)}</strong>
        </div>
      </section>
    </div>
  );
}
