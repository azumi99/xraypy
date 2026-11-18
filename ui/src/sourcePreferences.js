const key = (warehouse, view) => ["cas", "bgd"].includes(warehouse) && ["top", "side"].includes(view)
  ? `xray-source:${warehouse}:${view}` : null;

export function parseSourcePreference(raw) {
  try {
    const value = JSON.parse(raw);
    if (value?.kind === "screen") return { kind: "screen" };
    if (value?.kind === "browser" && typeof value.deviceId === "string" && value.deviceId.length > 0 && value.deviceId.length <= 1024) return { kind: "browser", deviceId: value.deviceId };
    if (typeof value?.kind === "string" && /^camera:\d+$/.test(value.kind)) return { kind: value.kind };
  } catch { /* invalid stored data */ }
  return null;
}

export function readSourcePreference(warehouse, view) {
  try { const k = key(warehouse, view); return k ? parseSourcePreference(localStorage.getItem(k)) : null; }
  catch { return null; }
}

export function writeSourcePreference(warehouse, view, preference) {
  try { const k = key(warehouse, view); if (k && parseSourcePreference(JSON.stringify(preference))) localStorage.setItem(k, JSON.stringify(preference)); }
  catch { /* storage may be unavailable */ }
}
