import { test } from "node:test";
import { deepStrictEqual, strictEqual } from "node:assert";
import { photoPath, photoRefs, photoSyncLabel, photoSyncPath, photoSyncPaths, photoSyncStatus } from "./photoSync.js";

test("collects only present displayed refs and encodes repeated parameters", () => {
  const refs = photoRefs([{ label_photo_ref: "label & 1", parcel: { crop_ref: "top/1", crop_ref_side: null } }, { label_photo_ref: "label & 1", parcel: { crop_ref_side: "side?2" } }, {}]);
  deepStrictEqual(refs, ["label & 1", "top/1", "side?2"]);
  deepStrictEqual(new URLSearchParams(photoSyncPath(refs).split("?")[1]).getAll("ref"), refs);
});

test("uses local photos first and reports unavailable sync honestly", () => {
  strictEqual(photoPath("cas/batch/photo 1.png"), "/crops/cas/batch/photo%201.png");
  strictEqual(photoPath("../unsafe.png"), "/api/sync/photo?ref=..%2Funsafe.png");
  strictEqual(photoSyncLabel(false, {}, "x"), "Sinkronisasi belum aktif");
  strictEqual(photoSyncLabel(null, {}, "x"), "Status sinkronisasi tidak tersedia");
  strictEqual(photoSyncLabel(true, { x: "synced" }, "x"), "Tersinkron dan terverifikasi");
  strictEqual(photoSyncLabel(true, { x: "available" }, "x"), "Tersedia di OBS");
  strictEqual(photoSyncLabel(true, { x: "stored" }, "x"), "Tersedia di server");
  strictEqual(photoSyncLabel(true, { x: "missing" }, "x"), "Belum tersedia di server");
});

test("batches status requests for large AWB groups", () => {
  const refs = Array.from({ length: 101 }, (_, i) => `batch/${i}.png`);
  const paths = photoSyncPaths(refs);
  deepStrictEqual(paths.flatMap((path) => new URLSearchParams(path.split("?")[1]).getAll("ref")), refs);
  for (const path of paths) assertLimit(path);
  function assertLimit(path) {
    strictEqual(new URLSearchParams(path.split("?")[1]).getAll("ref").length <= 30, true);
    strictEqual(path.length <= 3500, true);
  }
});

test("accepts only explicit sync statuses", () => {
  for (const status of ["pending", "synced", "failed", "unknown"]) strictEqual(photoSyncStatus({ ref: status }, "ref"), status);
  strictEqual(photoSyncStatus({}, "ref"), "unknown");
  strictEqual(photoSyncStatus({ ref: null }, "ref"), "unknown");
  strictEqual(photoSyncStatus({ ref: "unexpected" }, "ref"), "unknown");
});
