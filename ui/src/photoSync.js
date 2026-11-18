export function photoRefs(items) {
  return [...new Set(items.flatMap((item) => [item.label_photo_ref, item.parcel?.crop_ref, item.parcel?.crop_ref_side]).filter(Boolean))];
}

export function photoSyncPath(refs) {
  const params = new URLSearchParams();
  refs.forEach((ref) => params.append("ref", ref));
  return `/sync/status?${params}`;
}

export function photoSyncStatus(items, ref) {
  const status = Object.hasOwn(items ?? {}, ref) ? items[ref] : undefined;
  return ["pending", "synced", "failed", "unknown"].includes(status) ? status : "unknown";
}
