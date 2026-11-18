import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import { post } from "../api";
import { readSourcePreference, writeSourcePreference } from "../sourcePreferences";

const SourceSelect = forwardRef(function SourceSelect({ warehouse, view, value, cameras, onSaved }, ref) {
  const [preference, setPreference] = useState(() => readSourcePreference(warehouse, view));
  const [devices, setDevices] = useState([]);
  const [sharing, setSharing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const active = useRef({ generation: 0 });
  const mounted = useRef(true);
  const kind = preference?.kind === "browser" || preference?.kind === "screen" || cameras.includes(preference?.kind)
    ? preference.kind : (cameras.includes(value) ? value : "screen");

  const cleanup = () => {
    const state = active.current;
    state.generation++;
    clearInterval(state.timer);
    if (state.ws) { state.ws.onclose = null; state.ws.close(); }
    state.stream?.getTracks().forEach((track) => track.stop());
    if (state.video) state.video.srcObject = null;
    active.current = { generation: state.generation };
    if (mounted.current) { setSharing(false); setBusy(false); }
  };

  useEffect(() => {
    mounted.current = true;
    const media = navigator.mediaDevices;
    const changed = () => {
      if (active.current.stream && active.current.stream.getVideoTracks().every((track) => track.readyState === "ended")) {
        cleanup();
        setError("Perangkat terputus. Pilih sumber lagi.");
      }
      if (media?.enumerateDevices && active.current.authorized) refreshDevices().catch(() => {});
    };
    media?.addEventListener?.("devicechange", changed);
    return () => { mounted.current = false; media?.removeEventListener?.("devicechange", changed); cleanup(); };
  }, []);

  const refreshDevices = async () => {
    const list = await navigator.mediaDevices.enumerateDevices();
    if (mounted.current) setDevices(list.filter((device) => device.kind === "videoinput" && device.deviceId));
  };

  const discover = async () => {
    cleanup();
    setError(""); setBusy(true);
    const generation = active.current.generation;
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
      if (generation !== active.current.generation) return;
      await refreshDevices();
      if (generation === active.current.generation) active.current.authorized = true;
    } catch { if (generation === active.current.generation) setError(window.isSecureContext ? "Tidak dapat mengakses daftar kamera. Periksa izin browser." : "Kamera browser memerlukan HTTPS atau localhost."); }
    finally { stream?.getTracks().forEach((track) => track.stop()); if (mounted.current && generation === active.current.generation) setBusy(false); }
  };

  const selectKind = async (next) => {
    cleanup(); setError("");
    const nextPreference = next === "browser" ? { kind: next, deviceId: preference?.deviceId || "" } : { kind: next };
    setPreference(nextPreference);
    if (next === "screen" || next === "browser") {
      writeSourcePreference(warehouse, view, nextPreference);
      return;
    }
    setBusy(true);
    const generation = active.current.generation;
    try {
      await post("/sources", { view, kind: next });
      if (generation !== active.current.generation) return;
      writeSourcePreference(warehouse, view, nextPreference); onSaved?.();
    } catch { if (generation === active.current.generation) setError("Gagal memilih sumber server."); }
    finally { if (mounted.current && generation === active.current.generation) setBusy(false); }
  };

  const start = async () => {
    if (sharing) return true;
    if (kind === "browser" && !preference?.deviceId) {
      setError("Pilih perangkat kamera terlebih dahulu.");
      return false;
    }
    cleanup(); setError(""); setBusy(true);
    const generation = active.current.generation;
    let stream;
    try {
      stream = kind === "browser"
        ? await navigator.mediaDevices.getUserMedia({ video: { deviceId: { exact: preference.deviceId } }, audio: false })
        : await navigator.mediaDevices.getDisplayMedia({ video: true, audio: false });
      if (generation !== active.current.generation) { stream.getTracks().forEach((t) => t.stop()); return false; }
      active.current.stream = stream;
      if (kind === "browser") refreshDevices().catch(() => {});
      const track = stream.getVideoTracks()[0];
      if (!track) throw new Error("No video track");
      track.onended = () => { if (generation === active.current.generation) { cleanup(); setError("Sumber video berhenti atau terputus."); } };
      const video = document.createElement("video");
      active.current.video = video;
      video.muted = true;
      video.playsInline = true;
      video.srcObject = stream;
      await video.play();
      if (generation !== active.current.generation) return false;
      const canvas = document.createElement("canvas");
      const ctx = canvas.getContext("2d");
      if (!ctx) throw new Error("Canvas unavailable");
      const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/push/${view}`);
      active.current.ws = ws;
      await new Promise((resolve, reject) => {
        const timeout = setTimeout(() => reject(new Error("WebSocket timeout")), 10000);
        ws.onopen = () => { clearTimeout(timeout); resolve(); };
        ws.onerror = ws.onclose = () => { clearTimeout(timeout); reject(new Error("WebSocket unavailable")); };
      });
      if (generation !== active.current.generation) return false;
      ws.onclose = () => { if (generation === active.current.generation) { cleanup(); setError("Koneksi video terputus."); } };
      ws.onerror = () => { if (generation === active.current.generation) { cleanup(); setError("Koneksi video gagal."); } };
      await post("/sources", { view, kind: "screen" });
      if (generation !== active.current.generation) return false;
      active.current.timer = setInterval(() => {
        if (ws.readyState !== WebSocket.OPEN || ws.bufferedAmount > 1_000_000 || !video.videoWidth || !video.videoHeight) return;
        if (canvas.width !== video.videoWidth || canvas.height !== video.videoHeight) {
          canvas.width = video.videoWidth;
          canvas.height = video.videoHeight;
        }
        ctx.drawImage(video, 0, 0);
        canvas.toBlob((blob) => { if (blob && ws.readyState === WebSocket.OPEN && generation === active.current.generation) ws.send(blob); }, "image/jpeg", 0.7);
      }, 100);
      const saved = kind === "browser" ? { kind, deviceId: preference.deviceId } : { kind };
      writeSourcePreference(warehouse, view, saved); onSaved?.(); setSharing(true); setBusy(false);
      return true;
    } catch (cause) {
      if (generation === active.current.generation) {
        cleanup();
        const message = !window.isSecureContext ? "Kamera dan screen share memerlukan HTTPS atau localhost."
          : cause?.name === "NotAllowedError" ? "Izin kamera atau screen share ditolak di browser."
          : cause?.name === "NotFoundError" || cause?.name === "OverconstrainedError" ? "Kamera tersimpan tidak ditemukan. Cari dan pilih kamera lagi."
          : cause?.name === "NotReadableError" ? "Kamera sedang digunakan atau tidak dapat dibuka."
          : "Tidak dapat memulai video. Periksa perangkat dan koneksi.";
        setError(message);
      }
      return false;
    }
  };

  useImperativeHandle(ref, () => ({
    prepare: async () => {
      if (busy) {
        setError("Tunggu proses sumber video selesai.");
        return false;
      }
      if (preference?.kind?.startsWith("camera:") && !cameras.includes(preference.kind)) {
        setError("Kamera tersimpan tidak tersedia untuk gudang ini. Pilih sumber lain.");
        return false;
      }
      if (kind === "browser") return start();
      if (value !== kind) {
        try {
          await post("/sources", { view, kind });
          onSaved?.();
        } catch {
          setError("Gagal memilih sumber server.");
          return false;
        }
      }
      return true;
    },
  }));

  return (
    <div className="source-control">
      <div className="source-control-row">
        <label className="source-label" htmlFor={`source-${view}`}>Sumber video</label>
        <select id={`source-${view}`} className="source-select" value={kind} disabled={busy} onChange={(e) => selectKind(e.target.value)}>
          <option value="screen">Screen Share</option>
          <option value="browser">Kamera browser</option>
          {cameras.map((c) => <option key={c} value={c}>Camera {c.split(":")[1]}</option>)}
        </select>
        {sharing && <span className="source-active"><span />Aktif</span>}
        {sharing && <button type="button" className="source-button" onClick={cleanup}>Hentikan</button>}
        {kind === "screen" && !sharing && <button type="button" className="source-button" disabled={busy} onClick={start}>Pilih layar</button>}
      </div>
      {kind === "browser" && <div className="source-camera-row">
        <select aria-label={`Kamera ${view}`} className="source-select source-device" value={preference?.deviceId || ""} disabled={busy || sharing} onChange={(e) => { const selected = { kind: "browser", deviceId: e.target.value }; setPreference(selected); writeSourcePreference(warehouse, view, selected); }}>
          <option value="">Pilih perangkat kamera</option>
          {preference?.deviceId && !devices.some((d) => d.deviceId === preference.deviceId) && <option value={preference.deviceId}>Kamera tersimpan</option>}
          {devices.map((d, i) => <option key={d.deviceId} value={d.deviceId}>{d.label || `Kamera ${i + 1}`}</option>)}
        </select>
        {!sharing && <button type="button" className="source-button" disabled={busy} onClick={discover}>Cari kamera</button>}
        {!sharing && <button type="button" className="source-button" disabled={busy || !preference?.deviceId} onClick={start}>Mulai</button>}
      </div>}
      <div className="source-actions">
        {kind === "screen" && !sharing && <span className="source-note">Pilih layar jika belum berbagi.</span>}
        {!sharing && ((kind === "screen" || kind === "browser") ? value !== "screen" : value !== kind) && <span className="source-note">Pilihan belum aktif di server.</span>}
      </div>
      {error && <span role="alert" className="source-error">{error}</span>}
    </div>
  );
});

export default SourceSelect;
