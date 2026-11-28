import { useEffect, useState } from "react";
import api from "../api";

const endpoint = "/session/capture";
const labels = {
  idle: "Pilih AWB yang akan dipindai.",
  armed: "Menunggu paket dan kedua tampak.",
  candidate: "Periksa kedua foto sebelum menyimpan.",
  unassigned: "Ada capture tanpa AWB. Pilih item hanya jika paketnya sudah dipastikan.",
  review: "Perlu review",
  manual: "Gambar area paket di kedua tampak.",
  saving: "Menyimpan capture…",
};

function CropBox({ image, label, box, onChange }) {
  const [anchor, setAnchor] = useState(null);

  function position(event) {
    const bounds = event.currentTarget.getBoundingClientRect();
    const clamp = (value) => Math.min(1, Math.max(0, value));
    return [clamp((event.clientX - bounds.left) / bounds.width), clamp((event.clientY - bounds.top) / bounds.height)];
  }

  function draw(event) {
    const [x, y] = position(event);
    onChange([Math.min(x, anchor[0]), Math.min(y, anchor[1]), Math.max(x, anchor[0]), Math.max(y, anchor[1])]);
  }

  return (
    <div>
      <p className="text-xs font-bold mb-1">{label}</p>
      <div className="relative w-fit max-w-full touch-none cursor-crosshair"
        onPointerDown={(event) => {
          event.currentTarget.setPointerCapture(event.pointerId);
          setAnchor(position(event));
          onChange(null);
        }}
        onPointerMove={(event) => { if (anchor) draw(event); }}
        onPointerUp={(event) => { if (anchor) draw(event); setAnchor(null); }}
        onPointerCancel={() => { setAnchor(null); onChange(null); }}>
        <img src={image} alt={label} draggable={false} className="block w-full" />
        {box && <div className="pointer-events-none absolute border-2 border-yellow-300 bg-yellow-300/10" style={{
          left: `${box[0] * 100}%`, top: `${box[1] * 100}%`,
          width: `${(box[2] - box[0]) * 100}%`, height: `${(box[3] - box[1]) * 100}%`,
        }} />}
      </div>
    </div>
  );
}

export default function CaptureSelection({ waiting, onSaved }) {
  const [capture, setCapture] = useState(null);
  const [awbId, setAwbId] = useState("");
  const [preview, setPreview] = useState(null);
  const [boxes, setBoxes] = useState([null, null]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    let timer;
    async function refresh() {
      try {
        const data = await api(endpoint, { cache: "no-store", signal: controller.signal });
        if (!controller.signal.aborted) setCapture(data);
      } catch {
        // Keep the last known revision. The server rejects stale actions.
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(refresh, 750);
      }
    }
    refresh();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [busy]);

  const revision = capture?.revision;
  const state = capture?.state;
  useEffect(() => {
    if (!["candidate", "manual", "unassigned"].includes(state)) return;
    const controller = new AbortController();
    api(`${endpoint}/preview?revision=${encodeURIComponent(revision)}`, { signal: controller.signal, cache: "no-store" })
      .then((data) => {
        if (!controller.signal.aborted) {
          setBoxes([null, null]);
          setPreview(data);
        }
      })
      .catch(() => {
        if (!controller.signal.aborted) setError("Preview gagal dimuat. Scan ulang atau bekukan frame kembali.");
      });
    return () => controller.abort();
  }, [revision, state]);

  async function act(action, fields = {}) {
    setBusy(true);
    setError("");
    try {
      const response = await fetch(`/api${endpoint}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, revision, ...fields }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Capture gagal diproses.");
      setCapture(result);
      if (["confirm", "manual"].includes(action) && !result.awb) onSaved();
    } catch (failure) {
      setError(failure.message);
    } finally {
      setBusy(false);
    }
  }

  if (!capture) return null;
  const images = preview?.revision === revision ? preview.images : null;
  const active = capture.awb;
  const canSelect = ["idle", "unassigned"].includes(state);
  const validBoxes = boxes.every((box) => box && box[2] > box[0] && box[3] > box[1]);
  return (
    <div className="my-3 grid gap-3 rounded-xl border p-3">
      <div role="status">
        <p className="text-sm font-bold">{labels[state]}</p>
        {active && <p className="text-xs">HAWB <strong>{active.hawb || "—"}</strong> · MAWB {active.mawb || "—"} · Part {active.part ?? "—"} / {active.total ?? "—"}</p>}
        {capture.reason && <p className="text-xs">{capture.reason}</p>}
      </div>
      {error && <p role="alert" className="source-error">{error}</p>}
      {canSelect && <div className="flex flex-wrap gap-2">
        <label className="text-xs font-bold">Item AWB
          <select className="field mt-1" value={awbId} onChange={(event) => setAwbId(event.target.value)} disabled={busy}>
            <option value="">Pilih item dari Daftar Scan</option>
            {waiting.map((item) => <option key={item.id} value={item.id}>{item.hawb_number || item.awb_number} · Part {item.part ?? "—"}</option>)}
          </select>
        </label>
        <button type="button" className="btn-clay self-end" disabled={busy || !waiting.some((item) => String(item.id) === awbId)}
          onClick={() => act("select", { awb_id: Number(awbId) })}>Pilih AWB</button>
      </div>}
      {images && <div className="grid gap-3 md:grid-cols-2">
        {images.map((encoded, index) => {
          const label = index ? "Tampak Samping" : "Tampak Atas";
          const image = `data:image/jpeg;base64,${encoded}`;
          return state === "manual" ? <CropBox key={label} image={image} label={label} box={boxes[index]}
            onChange={(box) => setBoxes((current) => current.map((value, i) => i === index ? box : value))} />
            : <figure key={label}><figcaption className="text-xs">{label}</figcaption><img src={image} alt={label} /></figure>;
        })}
      </div>}
      <div className="flex flex-wrap gap-2">
        {state === "candidate" && <button type="button" className="btn-clay" disabled={busy || !images}
          onClick={() => act("confirm")}>Konfirmasi dan simpan</button>}
        {state === "manual" && <button type="button" className="btn-clay" disabled={busy || !images || !validBoxes}
          onClick={() => act("manual", { boxes })}>Simpan capture manual</button>}
        {state === "armed" && <button type="button" className="btn-ghost" disabled={busy}
          onClick={() => act("fail")}>Tandai gagal capture</button>}
        {active && ["armed", "candidate", "review", "manual"].includes(state) && <>
          <button type="button" className="btn-ghost" disabled={busy || state === "manual"} onClick={() => act("freeze")}>
            Capture Manual
          </button>
          <button type="button" className="btn-ghost" disabled={busy} onClick={() => act("retry")}>Scan ulang</button>
        </>}
        {state !== "idle" && state !== "saving" && <button type="button" className="btn-ghost" disabled={busy}
          onClick={() => act("skip")}>{active ? "Lewati" : "Buang kandidat"}</button>}
      </div>
    </div>
  );
}
