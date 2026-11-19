export function photoRefs(items) {
  return [...new Set(items.flatMap((item) => [item.label_photo_ref, item.parcel?.crop_ref, item.parcel?.crop_ref_side]).filter(Boolean))];
}

export function photoSyncPath(refs) {
  const params = new URLSearchParams();
  refs.forEach((ref) => params.append("ref", ref));
  return `/sync/status?${params}`;
}

export function photoSyncPaths(refs) {
  const paths = [];
  let batch = [];
  for (const ref of refs) {
    const next = [...batch, ref];
    if (batch.length && (next.length > 30 || photoSyncPath(next).length > 3500)) {
      paths.push(photoSyncPath(batch));
      batch = [ref];
    } else batch = next;
  }
  if (batch.length) paths.push(photoSyncPath(batch));
  return paths;
}

export function photoSyncStatus(items, ref) {
  const status = Object.hasOwn(items ?? {}, ref) ? items[ref] : undefined;
  return ["pending", "synced", "failed", "unknown", "stored", "missing"].includes(status) ? status : "unknown";
}

export function photoPath(ref) {
  const parts = ref.split("/");
  if (parts.some((part) => !part || part === "." || part === ".." || part.includes("\\"))) {
    return `/api/sync/photo?ref=${encodeURIComponent(ref)}`;
  }
  return `/crops/${parts.map(encodeURIComponent).join("/")}`;
}

export function photoSyncLabel(enabled, items, ref) {
  if (enabled === false) return "Sinkronisasi belum aktif";
  if (enabled !== true) return "Status sinkronisasi tidak tersedia";
  return { pending: "Menunggu sinkronisasi", synced: "Tersinkron", failed: "Sinkronisasi gagal atau foto belum final", unknown: "Belum terdaftar", stored: "Tersedia di server", missing: "Belum tersedia di server" }[photoSyncStatus(items, ref)];
}
