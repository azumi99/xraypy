// AWBs sharing both waybills use the same manifest result.
export function groupManifestRequests(items) {
  const groups = new Map();
  for (const item of items) {
    const key = JSON.stringify([item.awb_number, item.hawb_number]);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item.id);
  }
  return [...groups.values()];
}
