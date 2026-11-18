export function groupAwbs(items, manifests) {
  const groups = new Map();
  for (const item of items) {
    const key = item.awb_number?.trim() || `missing:${item.id}`;
    if (!groups.has(key)) groups.set(key, { mawb: item.awb_number, items: [] });
    groups.get(key).items.push(item);
  }
  return [...groups.values()].map((group) => {
    const quantities = new Map();
    let unavailable = false;
    let loading = false;
    for (const item of group.items) {
      const hawb = item.hawb_number?.trim();
      const manifest = manifests[item.id];
      if (!hawb) { unavailable = true; continue; }
      if (!manifest) { loading = true; continue; }
      const raw = manifest.data?.quantity;
      const quantity = typeof raw === "number" || (typeof raw === "string" && raw.trim()) ? Number(raw) : NaN;
      if (manifest.status !== "found" || !Number.isSafeInteger(quantity) || quantity <= 0 ||
          (quantities.has(hawb) && quantities.get(hawb) !== quantity)) {
        unavailable = true;
      } else {
        quantities.set(hawb, quantity);
      }
    }
    const scanned = group.items.filter((item) => item.status === "PAIRED").length;
    const airlineSet = new Set(), originSet = new Set();
    let airlinesUnknown = false, originUnknown = false;
    for (const item of group.items) {
      const manifest = manifests[item.id];
      const airline = manifest?.status === "found" ? String(manifest.data?.airlines_code ?? "").trim() : "";
      const origin = manifest?.status === "found" ? String(manifest.data?.origin ?? "").trim() : "";
      if (airline) airlineSet.add(airline); else airlinesUnknown = true;
      if (origin) originSet.add(origin); else originUnknown = true;
    }
    const join = (set, unknown) => set.size === 0 ? (unknown ? null : "") : [...set].sort().join(", ");
    const airlines = join(airlineSet, airlinesUnknown);
    const origin = join(originSet, originUnknown);
    const sum = [...quantities.values()].reduce((a, b) => a + b, 0);
    const expected = !unavailable && !loading && Number.isSafeInteger(sum) && sum > 0 ? sum : null;
    const status = unavailable || (!loading && expected === null) ? "UNAVAILABLE" : loading ? "LOADING" :
      scanned === expected ? "COMPLETE" : scanned < expected ? "INCOMPLETE" : "OVER EXPECTED";
    return { ...group, scanned, expected, status, airlines, origin };
  });
}
