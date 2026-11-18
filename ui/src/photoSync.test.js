import { test } from "node:test";
import { deepStrictEqual, strictEqual } from "node:assert";
import { photoRefs, photoSyncPath, photoSyncStatus } from "./photoSync.js";

test("collects only present displayed refs and encodes repeated parameters", () => {
  const refs = photoRefs([{ label_photo_ref: "label & 1", parcel: { crop_ref: "top/1", crop_ref_side: null } }, { label_photo_ref: "label & 1", parcel: { crop_ref_side: "side?2" } }, {}]);
  deepStrictEqual(refs, ["label & 1", "top/1", "side?2"]);
  deepStrictEqual(new URLSearchParams(photoSyncPath(refs).split("?")[1]).getAll("ref"), refs);
});

test("accepts only explicit sync statuses", () => {
  for (const status of ["pending", "synced", "failed", "unknown"]) strictEqual(photoSyncStatus({ ref: status }, "ref"), status);
  strictEqual(photoSyncStatus({}, "ref"), "unknown");
  strictEqual(photoSyncStatus({ ref: null }, "ref"), "unknown");
  strictEqual(photoSyncStatus({ ref: "unexpected" }, "ref"), "unknown");
});
