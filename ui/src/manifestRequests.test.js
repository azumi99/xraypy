import assert from "node:assert/strict";
import { test } from "node:test";
import { groupManifestRequests } from "./manifestRequests.js";

test("only AWBs with the same MAWB and HAWB share a manifest request", () => {
  const items = [
    { id: 1, awb_number: "M1", hawb_number: "H1" },
    { id: 2, awb_number: "M1", hawb_number: "H1" },
    { id: 3, awb_number: "M1", hawb_number: "H2" },
    { id: 4, awb_number: "M2", hawb_number: "H1" },
  ];
  assert.deepEqual(groupManifestRequests(items), [[1, 2], [3], [4]]);
});
