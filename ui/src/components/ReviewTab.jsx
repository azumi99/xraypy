import { useCallback, useEffect, useState } from "react";
import api, { post } from "../api";

export default function ReviewTab({ refreshKey }) {
  const [items, setItems] = useState([]);

  const load = useCallback(async () => {
    try {
      const d = await api("/dedup/review");
      setItems(d.items);
    } catch { /* keep last state */ }
  }, []);

  useEffect(() => { load(); }, [refreshKey, load]);

  const resolve = async (id, action) => {
    await post(`/dedup/${id}/resolve`, { action, operator: "operator" });
    await load();
  };

  return (
    <div className="panel">
      <h3>Paket zona abu-abu (REVIEW)</h3>
      {items.length === 0 && <p className="muted">Tidak ada paket yang menunggu review.</p>}
      {items.map((it) => (
        <div key={it.id} className="rev-card">
          {it.crop_ref && <img src={`/crops/${it.crop_ref}`} title="paket baru (atas)" alt="" />}
          {it.crop_ref_side && <img src={`/crops/${it.crop_ref_side}`} title="paket baru (samping)" alt="" />}
          {it.counterpart && <img src={`/crops/${it.counterpart.crop_ref}`} title={`kandidat lama (idx ${it.counterpart.idx})`} alt="" />}
          <div className="flex-1">
            <div>
              <span className="st-review badge">REVIEW</span>{" "}
              <span className="font-bold">seq/track {it.track_id}</span> · id {it.id}
              {it.sim_score != null && <> · sim {Number(it.sim_score).toFixed(3)}</>}
            </div>
            <div className="muted text-[11.5px]">
              {it.review_reason ?? ""}
              {it.counterpart
                ? ` · kandidat duplikat: id ${it.counterpart.id} (idx ${it.counterpart.idx}, ${it.counterpart.awb_number ?? "tanpa AWB"})`
                : " · tanpa kandidat (embedding gagal)"}
            </div>
            <div className="mt-2 flex gap-2 flex-wrap">
              <button className="btn-clay" onClick={() => resolve(it.id, "NOT_DUPLICATE")}>
                Bukan Duplikat → NEW
              </button>
              <button className="btn-clay-danger" onClick={() => resolve(it.id, "CONFIRM_DUPLICATE")}>
                Konfirmasi Duplikat
              </button>
              <button className="btn-ghost" onClick={() => resolve(it.id, "RESCAN_INTENTIONAL")}>
                Rescan Disengaja
              </button>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
