import { useCallback, useEffect, useRef, useState } from "react";

/**
 * WebSocket stream: latest annotated frames (top + side POV), overlay states,
 * and realtime events (parcel.*, session.*, awb.*). Auto-reconnects.
 */
export function useStream() {
  const [frames, setFrames] = useState({ top: null, side: null });
  const [overlays, setOverlays] = useState([]);
  const [connected, setConnected] = useState(false);
  const [waitingEventsAvailable, setWaitingEventsAvailable] = useState(false);
  const [lastEvent, setLastEvent] = useState(null);
  const listeners = useRef(new Set());

  useEffect(() => {
    let ws, timer, active = true;
    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws/stream`);
      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        if (!active) return;
        setConnected(false);
        setWaitingEventsAvailable(false);
        timer = setTimeout(connect, 2000);
      };
      ws.onmessage = (ev) => {
        const m = JSON.parse(ev.data);
        if (m.type === "awb.waiting_live") setWaitingEventsAvailable(m.available === true);
        if (m.type === "frame") {
          setFrames({ top: m.data, side: m.side ?? null });
          setOverlays(m.overlays ?? []);
        } else {
          if (m.type === "session.stopped" || m.type === "session.started") {
            // Do not reuse frames or overlays from an earlier session.
            setFrames({ top: null, side: null });
            setOverlays([]);
          }
          setLastEvent(m);
          listeners.current.forEach((fn) => fn(m));
        }
      };
    };
    connect();
    return () => {
      active = false;
      clearTimeout(timer);
      ws?.close();
    };
  }, []);

  const onEvent = useCallback((fn) => {
    listeners.current.add(fn);
    return () => listeners.current.delete(fn);
  }, []);

  return { frames, overlays, connected, waitingEventsAvailable, lastEvent, onEvent };
}
