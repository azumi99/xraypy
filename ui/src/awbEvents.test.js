import { test } from "node:test";
import assert from "node:assert/strict";
import { shouldRefreshAwbs } from "./awbEvents.js";

test("refreshes AWBs only for changes affecting the list", () => {
  for (const type of ["awb.inserted", "awb.deleted", "awb.updated", "parcel.saved", "parcel.review_resolved"]) assert.equal(shouldRefreshAwbs({ type }), true);
  for (const type of ["frame", "session.started", "parcel.duplicate"]) assert.equal(shouldRefreshAwbs({ type }), false);
  assert.equal(shouldRefreshAwbs(null), false);
});
