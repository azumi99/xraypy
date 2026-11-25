export function groupAwbs(items, manifests, { byHawb = false } = {}) {
  const groups = new Map();
  for (const item of items) {
    const mawb = item.awb_number?.trim();
    const hawb = item.hawb_number?.trim();
    const key = byHawb && mawb && hawb ? JSON.stringify([mawb, hawb])
      : byHawb || !mawb ? `missing:${item.id}` : mawb;
    if (!groups.has(key)) groups.set(key, { mawb: item.awb_number, items: [] });
    groups.get(key).items.push(item);
  }
  return [...groups.values()].map((group) => {
    const quantities = new Map();
    const hawbs = new Map();
    let unavailable = false;
    let loading = false;
    for (const item of group.items) {
      const hawb = item.hawb_number?.trim();
      const manifest = manifests[item.id];
      const key = hawb || `missing:${item.id}`;
      if (!hawbs.has(key)) hawbs.set(key, {
        hawb, items: [], scanned: 0, nomorAju: new Set(), incompleteManifest: false,
      });
      const detail = hawbs.get(key);
      detail.items.push(item);
      if (item.status === "PAIRED") detail.scanned += 1;
      const nomorAju = manifest?.status === "found" ? String(manifest.data?.nomor_aju ?? "").trim() : "";
      if (nomorAju) detail.nomorAju.add(nomorAju);
      if (!hawb) { unavailable = true; continue; }
      if (!manifest) { loading = true; detail.incompleteManifest = true; continue; }
      const raw = manifest.data?.quantity;
      const quantity = typeof raw === "number" || (typeof raw === "string" && raw.trim()) ? Number(raw) : NaN;
      if (manifest.status !== "found" || !Number.isSafeInteger(quantity) || quantity <= 0 ||
          (quantities.has(hawb) && quantities.get(hawb) !== quantity)) {
        unavailable = true;
        detail.incompleteManifest = true;
      } else {
        quantities.set(hawb, quantity);
      }
    }
    const scanned = group.items.filter((item) => item.status === "PAIRED").length;
    const airlineSet = new Set(), originSet = new Set(), destinationSet = new Set();
    let airlinesUnknown = false, originUnknown = false;
    for (const item of group.items) {
      const manifest = manifests[item.id];
      const airline = manifest?.status === "found" ? String(manifest.data?.airlines_code ?? "").trim() : "";
      const origin = manifest?.status === "found" ? String(manifest.data?.origin ?? "").trim() : "";
      const destination = manifest?.status === "found" ? String(manifest.data?.destination ?? "").trim() : "";
      if (destination) destinationSet.add(destination);
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
    const hawbGroups = [...hawbs.values()].map((detail) => ({
      hawb: detail.hawb, items: detail.items, scanned: detail.scanned,
      expected: detail.incompleteManifest ? null : quantities.get(detail.hawb) ?? null,
      nomorAju: [...detail.nomorAju].sort(),
    }));
    return { ...group, scanned, expected, status, airlines, origin, destination: [...destinationSet].sort().join(", "), hawbGroups };
  });
}
