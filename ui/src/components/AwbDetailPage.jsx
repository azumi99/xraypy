import { useEffect, useState } from "react";
import api, { del } from "../api";
import { useAwbDisplayStore } from "../store/awbDisplay";
import { photoPath, photoRefs, photoSyncLabel, photoSyncPaths } from "../photoSync";

const photoUrl = (ref) => `/api/sync/photo?ref=${encodeURIComponent(ref)}`;

function PhotoImage({ reference, alt, frame }) {
  const local = photoPath(reference);
  const remote = photoUrl(reference);
  const [src, setSrc] = useState(local);
  const [failed, setFailed] = useState(false);
  const [openError, setOpenError] = useState("");

  async function openFramedImage(event) {
    event.preventDefault();
    setOpenError("");
    const image = event.currentTarget.querySelector("img");
    if (!image.complete || !image.naturalWidth) {
      setOpenError("Tunggu hingga gambar selesai dimuat.");
      return;
    }
    const preview = window.open("", "_blank");
    if (!preview) {
      setOpenError("Izinkan pop-up untuk membuka gambar.");
      return;
    }
    preview.opener = null;
    try {
      const fonts = await document.fonts.load("700 12px Poppins");
      if (preview.closed) return;
      if (!fonts.length) throw new Error("Poppins unavailable");
      const canvas = document.createElement("canvas");
      const width = Math.max(640, image.naturalWidth);
      const imageHeight = Math.round(image.naturalHeight * width / image.naturalWidth);
      const scale = width / Math.max(320, image.naturalWidth);
      const titleHeight = 14;
      const textPadding = 8;
      const header = Math.ceil(33 * scale);
      canvas.width = width;
      canvas.height = header + imageHeight;
      const ctx = canvas.getContext("2d");
      if (!ctx) throw new Error("Canvas unavailable");
      ctx.save();
      ctx.scale(scale, scale);
      const frameWidth = width / scale;
      ctx.fillStyle = "#152630";
      ctx.fillRect(0, 0, frameWidth, titleHeight);
      ctx.textBaseline = "middle";
      ctx.font = "700 6px Poppins";
      ctx.fillStyle = "#f2f6fa";
      ctx.fillText("Xray Cargo", textPadding, titleHeight / 2);
      ctx.textAlign = "right";
      ctx.fillStyle = "#f2cb79";
      ctx.fillText(frame.caption, frameWidth - textPadding, titleHeight / 2, frameWidth - 74);
      ctx.textAlign = "left";
      ctx.fillStyle = "#f8fafb";
      ctx.fillRect(0, titleHeight, frameWidth, header / scale - titleHeight);
      ctx.fillStyle = "#d8ab51";
      ctx.fillRect(0, header / scale - 2, frameWidth, 2);
      const column = frameWidth / frame.fields.length;
      frame.fields.forEach(([label, value], index) => {
        const left = index * column;
        if (index) {
          ctx.fillStyle = "#cbd4da";
          ctx.fillRect(left, titleHeight, 1, header / scale - titleHeight - 2);
        }
        ctx.fillStyle = "#34434c";
        ctx.fillText(label, left + textPadding, titleHeight + 5, column - 2 * textPadding);
        ctx.fillStyle = "#152630";
        ctx.fillText(value, left + textPadding, titleHeight + 13, column - 2 * textPadding);
      });
      ctx.restore();
      ctx.drawImage(image, 0, header, width, imageHeight);
      canvas.toBlob((blob) => {
        if (preview.closed) return;
        if (!blob) {
          preview.close();
          setOpenError("Gambar dengan bingkai tidak dapat dibuat.");
          return;
        }
        const url = URL.createObjectURL(blob);
        preview.location.replace(url);
        // Keep the URL available while the image tab is open.
        const timer = window.setInterval(() => {
          if (preview.closed) {
            URL.revokeObjectURL(url);
            window.clearInterval(timer);
          }
        }, 5000);
      }, "image/png");
    } catch (error) {
      preview.close();
      setOpenError(error.message === "Poppins unavailable"
        ? "Font Poppins tidak dapat dimuat. Coba lagi."
        : "Gambar dengan bingkai tidak dapat dibuat.");
    }
  }

  if (failed) return <div className="awb-scan-empty" role="status">Foto tidak dapat dimuat</div>;
  const image = <img src={src} alt={alt} title="Lihat ukuran penuh"
    onError={() => { if (src !== remote) setSrc(remote); else setFailed(true); }} />;
  return frame ? <>
    <button type="button" className="awb-scan-image" onClick={openFramedImage}>{image}</button>
    {openError && <p role="alert" className="text-bad text-xs p-2">{openError}</p>}
  </> : <a className="awb-scan-image" href={src} target="_blank" rel="noreferrer">{image}</a>;
}

function XrayFrame({ item, parcel, reference, view }) {
  const side = view === "side";
  const timestamp = parcel?.created_at ? new Date(parcel.created_at) : null;
  const recordedAt = timestamp && !Number.isNaN(timestamp.getTime())
    ? timestamp.toLocaleString("id-ID", { timeZone: "Asia/Jakarta", dateStyle: "medium", timeStyle: "short" })
    : null;
  const caption = `${side ? "Tampak Samping" : "Tampak Atas"}${recordedAt ? ` · ${recordedAt} WIB` : ""}`;
  const fields = [
    ["HAWB", item.hawb_number || "—"],
    ["MAWB", item.awb_number || "—"],
    ["Part", `${item.part ?? "—"} / ${item.total ?? "—"}`],
  ];
  return (
    <figure className="xray-frame">
      <figcaption>
        <div className="xray-frame-header">
          <strong>Xray Cargo</strong>
          <span>{caption}</span>
        </div>
        <dl className="xray-frame-meta">
          {fields.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
        </dl>
      </figcaption>
      <PhotoImage key={reference} reference={reference} frame={{ caption, fields }}
        alt={`X-ray ${side ? "tampak samping" : "tampak atas"} ${item.hawb_number ?? item.id}`} />
    </figure>
  );
}


/** Halaman detail AWB (route /awb/:id) — foto 2 POV + info parcel + audit. */
export default function AwbDetailPage({ id, onBack, onChanged, readOnly = false }) {
  const [data, setData] = useState(null);
  const [notFound, setNotFound] = useState(false);
  const [audit, setAudit] = useState([]);
  const [manifest, setManifest] = useState(null);
  const [manifestAttempt, setManifestAttempt] = useState(0);
  const [error, setError] = useState(false);
  const [groupItems, setGroupItems] = useState([]);
  const [syncItems, setSyncItems] = useState({});
  const [syncEnabled, setSyncEnabled] = useState(null);
  const grouped = useAwbDisplayStore((state) => state.grouped);

  const displayedItems = data && !notFound && !error && String(data.id) === String(id)
    ? (grouped && groupItems.length ? groupItems.map((item) => String(item.id) === String(data.id) ? data : item) : [data]) : [];
  const refsKey = JSON.stringify(photoRefs(displayedItems));

  useEffect(() => {
    const refs = JSON.parse(refsKey);
    let active = true;
    let inFlight = false;
    setSyncItems({});
    setSyncEnabled(null);
    if (!refs.length) return () => { active = false; };
    const refresh = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const queue = photoSyncPaths(refs);
        const results = [];
        const worker = async () => { while (queue.length) results.push(await api(queue.shift())); };
        await Promise.all(Array.from({ length: Math.min(3, queue.length) }, worker));
        if (active) {
          setSyncItems(Object.assign({}, ...results.map((result) => result?.items ?? {})));
          setSyncEnabled(results.every((result) => result?.enabled === true) ? true : results.every((result) => result?.enabled === false) ? false : null);
        }
      } catch {
        if (active) { setSyncItems({}); setSyncEnabled(null); }
      } finally {
        inFlight = false;
      }
    };
    refresh();
    const timer = setInterval(refresh, 15000);
    return () => { active = false; clearInterval(timer); };
  }, [refsKey]);

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
      const hawb = data.hawb_number?.trim();
      const members = hawb ? result.items.filter((item) =>
        item.awb_number?.trim() === data.awb_number.trim() && item.hawb_number?.trim() === hawb,
      ).sort((x, y) => (x.part ?? Infinity) - (y.part ?? Infinity) || x.id - y.id) : [data];
      setGroupItems(members.length ? members : [data]);
    }).catch(() => { if (active) setGroupItems([data]); });
    return () => { active = false; };
  }, [id, data?.id, data?.awb_number, data?.hawb_number, grouped]);

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

  const statusColor = { PAIRED: "st-PAIRED", WAITING: "st-WAITING", CANCELLED: "st-CANCELLED" };

  return (
    <div className="awb-detail awb-detail-redesign">
      <div className="awb-detail-header">
        <div className="awb-shipment-heading">
          <span className="muted text-xs">HAWB</span>
          <h2 className="awb-detail-title">{data.hawb_number || data.awb_number}</h2>
          <p className="muted text-sm">MAWB <strong>{data.awb_number}</strong>
            {manifest?.data?.nomor_aju && <> · Nomor AJU <strong>{manifest.data.nomor_aju}</strong></>}
          </p>
        </div>
        {!readOnly && (data.status !== "PAIRED" ? (
          <button className="awb-delete" onClick={async () => { await del(`/awb/${id}`); onChanged?.(); onBack(); }}>
            Hapus AWB
          </button>
        ) : (
          <button className="awb-delete" onClick={forceDelete}>
            Hapus dengan Force
          </button>
        ))}
      </div>

      <section className="awb-scan-panel" aria-labelledby="awb-captures-title">
        <div className="awb-detail-section-title">
          <h3 id="awb-captures-title">Hasil pemindaian</h3>
          <span className="awb-capture-summary">
            <strong>{displayedItems.filter((item) => item.parcel?.crop_ref && item.parcel?.crop_ref_side).length} / {displayedItems.length}</strong> bagian dengan dua foto X-ray
          </span>
        </div>
        {displayedItems.map((item) => {
          const parcel = item.parcel;
          const status = item.status === "PAIRED" ? "COMPLETE" : item.status;
          return (
            <article className="awb-capture-card" key={item.id} aria-labelledby={`awb-capture-${item.id}`}>
              <header className="awb-detail-section-title awb-capture-heading">
                <h4 id={`awb-capture-${item.id}`}>Part {item.part ?? "—"} / {item.total ?? "—"}</h4>
                <span className={`awb-status ${statusColor[item.status] ?? ""}`}>{status}</span>
              </header>
              <div className="awb-detail-grid awb-capture-grid">
                <div className="awb-scans">
                  <div className="awb-scan">
                    {parcel?.crop_ref ? (
                      <XrayFrame item={item} parcel={parcel} reference={parcel.crop_ref} view="top" />
                    ) : <div className="awb-scan-empty">Belum ada foto tampak atas</div>}
                    {parcel?.crop_ref && <div className="muted text-xs">{photoSyncLabel(syncEnabled, syncItems, parcel.crop_ref)}</div>}
                  </div>
                  <div className="awb-scan">
                    {parcel?.crop_ref_side ? (
                      <XrayFrame item={item} parcel={parcel} reference={parcel.crop_ref_side} view="side" />
                    ) : <div className="awb-scan-empty">Belum ada foto tampak samping</div>}
                    {parcel?.crop_ref_side && <div className="muted text-xs">{photoSyncLabel(syncEnabled, syncItems, parcel.crop_ref_side)}</div>}
                  </div>
                </div>
                <section className="awb-capture-info" aria-label={`Informasi AWB #${item.id}`}>
                  <h5>Informasi AWB</h5>
                  <dl className="awb-info-list">
                    {[
                      ["MAWB", item.awb_number], ["HAWB", item.hawb_number],
                      ["Dibuat", item.created_at], ["Paired", item.paired_at],
                      ["Waktu capture", parcel?.created_at],
                    ].map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value ?? "—"}</dd></div>)}
                  </dl>
                  {parcel && <details className="awb-capture-disclosure">
                    <summary>Data teknis</summary>
                    <dl className="awb-info-list">
                      {[
                        ["Parcel ID", parcel.id], ["Idx / seq track", `${parcel.idx ?? "—"} / ${parcel.track_id ?? "—"}`],
                        ["Status paket", parcel.status], ["Batch", parcel.batch_id],
                        ["Similarity", parcel.sim_score != null ? Number(parcel.sim_score).toFixed(4) : null],
                      ].map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value ?? "—"}</dd></div>)}
                    </dl>
                  </details>}
                  {item.label_photo_ref && <details className="awb-capture-disclosure">
                    <summary>Foto label QR</summary>
                    <PhotoImage key={item.label_photo_ref} reference={item.label_photo_ref} alt={`Foto label hasil scan QR ${item.hawb_number ?? item.id}`} />
                    <div className="muted text-xs">{photoSyncLabel(syncEnabled, syncItems, item.label_photo_ref)}</div>
                  </details>}
                </section>
              </div>
            </article>
          );
        })}
      </section>

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
          ["Dokumen", [["Nomor AJU", "nomor_aju"], ["BC11", "bc11"], ["Tanggal BC", "tgl_bc"], ["SPPB", "sppb"], ["Tanggal SPPB", "tgl_sppb"], ["Tanggal masuk", "date_entry"], ["Waktu masuk", "time_entry"]]],
        ].map(([title, fields]) => <div className="manifest-group" key={title}><h4>{title}</h4><dl>{fields.map(([label, key]) => <div key={key}><dt>{label}</dt><dd>{manifest.data[key] ?? "—"}</dd></div>)}</dl></div>)}</div>}
      </section>

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
