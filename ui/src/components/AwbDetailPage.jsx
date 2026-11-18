import { useEffect, useState } from "react";
import api, { del } from "../api";
import { useAwbDisplayStore } from "../store/awbDisplay";

/** Halaman detail AWB (route /awb/:id) — foto 2 POV + info parcel + audit. */
export default function AwbDetailPage({ id, onBack, onChanged }) {
  const [data, setData] = useState(null);
  const [notFound, setNotFound] = useState(false);
  const [audit, setAudit] = useState([]);
  const [manifest, setManifest] = useState(null);
  const [manifestAttempt, setManifestAttempt] = useState(0);
  const [error, setError] = useState(false);
  const [groupItems, setGroupItems] = useState([]);
  const grouped = useAwbDisplayStore((state) => state.grouped);

  const load = async () => {
    try {
      const d = await api(`/awb/${id}`);
      setData(d);
      setNotFound(false);
      setError(false);
      setAudit(d.audit ?? []);
    } catch (e) {
      if (e.message.includes("404")) setNotFound(true);
      else setError(true);
    }
  };

  useEffect(() => { load(); }, [id]);
  useEffect(() => {
    let active = true;
    setManifest(null);
    api(`/awb/${id}/manifest`).then((result) => { if (active) setManifest(result); })
      .catch(() => { if (active) setManifest({ status: "unavailable" }); });
    return () => { active = false; };
  }, [id, manifestAttempt]);

  useEffect(() => {
    let active = true;
    setGroupItems([]);
    if (!grouped || !data?.awb_number || String(data.id) !== String(id)) return () => { active = false; };
    api("/awb").then((result) => {
      if (!active) return;
      const members = result.items.filter((item) => item.awb_number === data.awb_number).sort((x, y) => x.id - y.id);
      setGroupItems(members.length ? members : [data]);
    }).catch(() => { if (active) setGroupItems([data]); });
    return () => { active = false; };
  }, [id, data?.id, data?.awb_number, grouped]);

  const forceDelete = async () => {
    if (!confirm("Hapus AWB ini dengan force? Paket terkait akan menjadi TANPA AWB dan tercatat di audit log.")) return;
    await del(`/awb/${id}?force=true`);
    onChanged?.();
    onBack();
  };

  if (notFound) {
    return (
      <div className="panel">
        <p className="muted">AWB tidak ditemukan.</p>
        <button className="btn-ghost mt-3" onClick={onBack}>← Kembali ke Queue AWB</button>
      </div>
    );
  }
  if (error) return <div className="panel" role="alert">Detail AWB tidak dapat dimuat. <button onClick={load}>Coba lagi</button></div>;
  if (!data) return <div className="panel muted">memuat…</div>;

  const p = data.parcel;
  const statusColor = { PAIRED: "st-PAIRED", WAITING: "st-WAITING", CANCELLED: "st-CANCELLED" };

  return (
    <div className="awb-detail">
      <div className="awb-detail-header">
        <div>
          <h2 className="awb-detail-title">
            {data.awb_number}
            <span className={`awb-status ${statusColor[data.status] ?? ""}`}>{data.status === "PAIRED" ? "COMPLETE" : data.status}</span>
          </h2>
        </div>
        {data.status !== "PAIRED" ? (
          <button className="awb-delete" onClick={async () => { await del(`/awb/${id}`); onChanged?.(); onBack(); }}>
            Hapus AWB
          </button>
        ) : (
          <button className="awb-delete" onClick={forceDelete}>
            Hapus dengan Force
          </button>
        )}
      </div>

      <section className="panel manifest-panel">
        <div className="manifest-heading"><div><span className="awb-eyebrow">DATA PENGIRIMAN</span><h3>Manifest</h3><p>Informasi berdasarkan MAWB dan HAWB.</p></div><span className="manifest-reference">{data.awb_number} <span>/</span> {data.hawb_number ?? "—"}</span></div>
        {manifest?.status !== "found" && <div className="manifest-empty" role={manifest?.status === "unavailable" ? "alert" : undefined}>
          <span>{!manifest ? "Memuat manifest..." : manifest.status === "missing_numbers" ? "MAWB dan HAWB diperlukan untuk mencari manifest." : manifest.status === "not_found" ? "Manifest tidak ditemukan untuk MAWB dan HAWB ini." : "Manifest tidak tersedia saat ini."}</span>
          {manifest?.status === "unavailable" && <button className="manifest-retry" onClick={() => setManifestAttempt((n) => n + 1)}>Coba lagi</button>}
        </div>}
        {manifest?.status === "found" && <div className="manifest-groups">{[
          ["Penerbangan", [["MAWB", "mawb"], ["HAWB", "hawb"], ["Airlines", "airlines_code"], ["Flight", "flight_no"], ["Tanggal penerbangan", "date_of_flight"], ["Asal", "origin"]]],
          ["Kargo", [["Jumlah", "quantity"], ["Berat", "weight"], ["Volume", "volume"], ["HS code", "hs_code"], ["Deskripsi", "description_goods"], ["Nomor bag", "bag_number"]]],
          ["Pengirim & Penerima", [["Pengirim", "shipper_name"], ["Kota pengirim", "shipper_city"], ["Penerima", "consignee_name"], ["Kota penerima", "consignee_city"], ["Negara penerima", "consignee_country"]]],
          ["Dokumen", [["BC11", "bc11"], ["Tanggal BC", "tgl_bc"], ["SPPB", "sppb"], ["Tanggal SPPB", "tgl_sppb"], ["Tanggal masuk", "date_entry"], ["Waktu masuk", "time_entry"]]],
        ].map(([title, fields]) => <div className="manifest-group" key={title}><h4>{title}</h4><dl>{fields.map(([label, key]) => <div key={key}><dt>{label}</dt><dd>{manifest.data[key] ?? "—"}</dd></div>)}</dl></div>)}</div>}
      </section>

      <div className="awb-detail-grid">
        <section className="panel awb-scan-panel">
          <div className="awb-detail-section-title"><div><span className="awb-eyebrow">HASIL PEMINDAIAN</span><h3>Foto X-ray</h3></div><span className="muted text-xs">Klik foto untuk ukuran penuh</span></div>
          {data.label_photo_ref && <div className="awb-scan-group">
            <div className="awb-scan-group-label"><span>Foto Label (QR)</span><span className="muted">{data.timecreated && !Number.isNaN(new Date(data.timecreated).getTime()) ? new Date(data.timecreated).toLocaleString("id-ID", { dateStyle: "medium", timeStyle: "short" }) : ""}</span></div>
            <div className="awb-scans">
              <div className="awb-scan">
                <div className="awb-scan-label"><span>QR</span> Label</div>
                <a className="awb-scan-image" href={`/crops/${data.label_photo_ref}`} target="_blank" rel="noreferrer"><img src={`/crops/${data.label_photo_ref}`} alt="Foto label hasil scan QR" title="Lihat ukuran penuh" /></a>
              </div>
            </div>
          </div>}
          {(grouped && groupItems.length ? groupItems : [data]).map((item) => {
            const parcel = item.id === data.id ? p : item.parcel;
            return <div className="awb-scan-group" key={item.id}>
              <div className="awb-scan-group-label"><span>{item.hawb_number || "HAWB tidak tersedia"} | Part {item.part ?? "—"} / Total {item.total ?? "—"}</span><span className="muted">#{item.id}</span></div>
              <div className="awb-scans">
                <div className="awb-scan">
                  <div className="awb-scan-label"><span>01</span> Tampak Atas</div>
                  {parcel?.crop_ref ? (
                    <a className="awb-scan-image" href={`/crops/${parcel.crop_ref}`} target="_blank" rel="noreferrer">
                      <img src={`/crops/${parcel.crop_ref}`} alt={`X-ray tampak atas ${item.hawb_number ?? item.id}`} title="Lihat ukuran penuh" />
                    </a>
                  ) : <div className="awb-scan-empty">Belum ada foto tampak atas</div>}
                </div>
                <div className="awb-scan">
                  <div className="awb-scan-label"><span>02</span> Tampak Samping</div>
                  {parcel?.crop_ref_side ? (
                    <a className="awb-scan-image" href={`/crops/${parcel.crop_ref_side}`} target="_blank" rel="noreferrer">
                      <img src={`/crops/${parcel.crop_ref_side}`} alt={`X-ray tampak samping ${item.hawb_number ?? item.id}`} title="Lihat ukuran penuh" />
                    </a>
                  ) : <div className="awb-scan-empty">Belum ada foto tampak samping</div>}
                </div>
              </div>
            </div>;
          })}
        </section>

        <section className="panel awb-info-panel">
          <span className="awb-eyebrow">DETAIL PENGIRIMAN</span>
          <h3>Informasi AWB</h3>
          <dl className="awb-info-list">
            {[
              ["Nomor AWB", data.awb_number], ["HAWB", data.hawb_number], ["Status AWB", data.status === "PAIRED" ? "COMPLETE" : data.status],
              ["Part", data.part], ["Total", data.total],
              ["Dibuat", data.created_at], ["Paired", data.paired_at],
              ["Parcel ID", p?.id], ["Idx / seq track", `${p?.idx ?? "—"} / ${p?.track_id ?? "—"}`],
              ["Status paket", p?.status], ["Batch", p?.batch_id],
              ["Similarity", p?.sim_score != null ? Number(p.sim_score).toFixed(4) : null],
              ["Waktu capture", p?.created_at],
            ].map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value ?? "—"}</dd></div>)}
          </dl>
        </section>
      </div>

      {audit.length > 0 && (
        <div className="panel">
          <h3>Riwayat Audit</h3>
          <table>
            <thead><tr><th>Waktu</th><th>Aksi</th><th>Operator</th><th>Alasan</th></tr></thead>
            <tbody>
              {audit.map((a) => (
                <tr key={a.id}>
                  <td className="muted">{a.at}</td>
                  <td>{a.action}</td>
                  <td>{a.operator}</td>
                  <td>{a.reason ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
