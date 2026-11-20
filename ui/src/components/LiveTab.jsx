import { useCallback, useEffect, useRef, useState } from "react";
import api, { post } from "../api";
import SourceSelect from "./SourceSelect";
import { shouldRefreshAwbs } from "../awbEvents";

export default function LiveTab({ warehouse, frames, session, onSessionChange, onEvent, connected, waitingEventsAvailable, visible }) {
  const topSource = useRef(null);
  const sideSource = useRef(null);
  const [starting, setStarting] = useState(false);
  const [sessionError, setSessionError] = useState("");
  const start = async () => {
    if (starting) return;
    if (session?.running && !confirm("Session masih berjalan. Hentikan sesi lama dan mulai sesi baru?")) return;
    setStarting(true);
    setSessionError("");
    try {
      // Begin browser camera requests directly from the user's click.
      const prepared = await Promise.all([topSource.current?.prepare(), sideSource.current?.prepare()]);
      if (prepared.some((ok) => ok !== true)) {
        setSessionError("Sumber video belum siap. Periksa pengaturan dan izin kamera di atas.");
        return;
      }
      if (session?.running) await post("/session/stop");
      await post("/session/start");
      await onSessionChange();
    } catch (error) {
      setSessionError(error instanceof Error ? error.message : "Tidak dapat memulai sesi.");
    } finally {
      setStarting(false);
    }
  };
  const stop = async () => {
    await post("/session/stop");
    onSessionChange();
  };

  const [sources, setSources] = useState({ top: "screen", side: "screen", cameras: [] });
  const [waiting, setWaiting] = useState([]);
  const [leaving, setLeaving] = useState([]);
  const [waitingError, setWaitingError] = useState(false);
  const refreshTimer = useRef(null);
  const waitingRequest = useRef(null);
  const requestVersion = useRef(0);
  const loadSources = async () => setSources(await api("/sources").catch(() => sources));
  useEffect(() => { if (visible) loadSources(); }, [visible]);

  const scheduleWaitingRefresh = useCallback((delay = 100) => {
    const version = ++requestVersion.current;
    clearTimeout(refreshTimer.current);
    refreshTimer.current = setTimeout(async () => {
      refreshTimer.current = null;
      waitingRequest.current?.abort();
      const request = new AbortController();
      waitingRequest.current = request;
      try {
        const data = await api("/awb/waiting", { cache: "no-store", signal: request.signal });
        if (requestVersion.current !== version) return;
        setWaiting(data.items);
        setWaitingError(false);
      } catch {
        if (requestVersion.current === version) setWaitingError(true);
      } finally {
        if (waitingRequest.current === request) waitingRequest.current = null;
        if (requestVersion.current === version) setLeaving([]);
      }
    }, delay);
  }, []);

  useEffect(() => {
    if (!visible) return;
    scheduleWaitingRefresh(0);
    const unsubscribe = onEvent((event) => {
      if (!shouldRefreshAwbs(event)) return;
      if (event.type === "parcel.saved" && event.awb_number) {
        setLeaving((current) => [...new Set([...current, event.awb_number])]);
        scheduleWaitingRefresh(650);
      } else {
        scheduleWaitingRefresh();
      }
    });
    return () => {
      unsubscribe();
      clearTimeout(refreshTimer.current);
      refreshTimer.current = null;
      requestVersion.current += 1;
      waitingRequest.current?.abort();
      waitingRequest.current = null;
    };
  }, [onEvent, scheduleWaitingRefresh, visible, warehouse]);
  useEffect(() => {
    if (visible && connected) scheduleWaitingRefresh(0);
  }, [connected, scheduleWaitingRefresh, visible]);

  useEffect(() => {
    // Keep queues fresh during a socket outage or a rolling backend update.
    if (!visible || waitingEventsAvailable) return;
    const poll = setInterval(() => {
      if (refreshTimer.current === null && waitingRequest.current === null) {
        scheduleWaitingRefresh(0);
      }
    }, 2000);
    return () => clearInterval(poll);
  }, [waitingEventsAvailable, scheduleWaitingRefresh, visible]);

  return (
    <div className="flex flex-wrap gap-5">
      <div className="panel flex-[1_1_480px] min-w-0">
        <div className="mb-4 flex flex-wrap items-center gap-2">
          <button className="btn-clay !px-4 !py-2 !text-xs" onClick={start} disabled={starting}>
            {starting ? "Menyiapkan…" : session?.running ? "Restart Session" : "Start Session"}
          </button>
          <button className="btn-clay-danger !px-4 !py-2 !text-xs" onClick={stop} disabled={starting}>
            Stop
          </button>
          <span className="muted text-[11.5px]">mode: {session?.mode ?? "-"}</span>
        </div>
        {sessionError && <p role="alert" className="source-error mb-3">{sessionError}</p>}
        <div className="grid gap-5 grid-cols-1">
          <VideoSection
            view="top" title="Tampak Atas" src={session?.running ? frames.top : null}
            label={session?.running ? "Menunggu sumber video Tampak Atas..." : "Mulai sesi untuk melihat video"}
            warehouse={warehouse} sources={sources} onSaved={loadSources} sourceRef={topSource}
          />
          <VideoSection
            view="side" title="Tampak Samping" src={session?.running ? frames.side : null}
            label={session?.running ? "Menunggu sumber video Tampak Samping..." : "Mulai sesi untuk melihat video"}
            warehouse={warehouse} sources={sources} onSaved={loadSources} sourceRef={sideSource}
          />
        </div>
      </div>

      <div className="panel flex-[1_1_480px] min-w-0">
        <div className="scan-list-heading">
          <div><span className="awb-eyebrow">ANTREAN X-RAY</span><h3>Daftar Scan</h3></div>
          <span className="awb-total">{waiting.length} waiting</span>
        </div>
        <p className="muted text-xs mt-2 mb-4">AWB diproses sesuai urutan antrean.</p>
        {waitingError && <p role="alert" className="source-error mb-3">Antrean AWB gagal dimuat. Data mungkin belum terbaru.</p>}
        <ol className="scan-list" aria-label="Antrean AWB menunggu scan">
          {waiting.map((awb, index) => (
            <li key={awb.id} className={`scan-list-item ${leaving.includes(awb.awb_number) ? "scan-list-item-leaving" : ""}`}>
              <span className="scan-list-index">{String(index + 1).padStart(2, "0")}</span>
              <span className="scan-list-number">
                <span className="scan-list-waybill"><span className="scan-list-waybill-label">MAWB</span><span>{awb.awb_number || "—"}</span></span>
                <span className="scan-list-waybill"><span className="scan-list-waybill-label">HAWB</span><span>{awb.hawb_number || "—"}</span></span>
                <span className="scan-list-pieces">{awb.part != null ? `Piece ${awb.part} dari ${awb.total ?? "—"}` : "Piece —"}</span>
              </span>
              <span className="scan-list-status">{leaving.includes(awb.awb_number) ? "Tersimpan" : "Waiting"}</span>
            </li>
          ))}
        </ol>
        {waiting.length === 0 && <div className="scan-list-empty">Belum ada AWB dalam antrean.</div>}
      </div>
    </div>
  );
}

export async function refreshSession() {
  return api("/session/state").catch(() => null);
}


function VideoSection({ view, title, src, label, warehouse, sources, onSaved, sourceRef }) {
  // rasio section mengikuti rasio frame sumber (diukur tiap frame masuk),
  // jadi demo / screen share / kamera selalu pas penuh — tanpa crop & distorsi
  const [ratio, setRatio] = useState("636 / 308");
  return (
    <section className="w-full min-w-0">
      <div className="video-source-header">
        <div className="flex items-center gap-2">
          <span className="w-2.5 h-2.5 rounded-full bg-[var(--cta)] border-2 border-[var(--text)]" />
          <h3 className="!mb-0 !text-[14px]">{title}</h3>
        </div>
        <SourceSelect ref={sourceRef} key={`${warehouse}-${view}`} warehouse={warehouse} view={view} value={sources[view]} cameras={sources.cameras ?? []} onSaved={onSaved} />
      </div>
      <div
        className="live-video-frame w-full rounded-[18px] overflow-hidden border-3 border-[var(--text)] bg-black shadow-[6px_6px_0_var(--text)]"
        style={{ aspectRatio: ratio }}
      >
        {src ? (
          <img
            className="block w-full h-full object-contain"
            alt={title}
            src={`data:image/jpeg;base64,${src}`}
            onLoad={(e) => {
              const r = `${e.target.naturalWidth} / ${e.target.naturalHeight}`;
              setRatio((prev) => (prev === r ? prev : r));
            }}
          />
        ) : (
          <div className="w-full h-full grid place-items-center text-muted bg-[var(--bg-cream)]">{label}</div>
        )}
      </div>
    </section>
  );
}
