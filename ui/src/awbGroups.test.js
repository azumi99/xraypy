import test from "node:test";
import assert from "node:assert/strict";
import { groupAwbs } from "./awbGroups.js";

const item = (id, hawb, status = "PAIRED", awb_number = "M") => ({ id, awb_number, hawb_number: hawb, status });
const found = (quantity, nomor_aju) => ({ status: "found", data: { quantity, nomor_aju } });

test("groups before counting and sums each HAWB once", () => {
  const items = [item(1, "H1"), item(2, "H1"), item(3, "H2", "WAITING"), item(4, "H3", "PAIRED", "OTHER")];
  const [group, other] = groupAwbs(items, { 1: found(2, "001"), 2: found("2", "001"), 3: found(1, "002"), 4: found(1) });
  assert.deepEqual([group.scanned, group.expected, group.status], [2, 3, "INCOMPLETE"]);
  assert.deepEqual(group.hawbGroups.map(({ hawb, scanned, expected, nomorAju }) =>
    [hawb, scanned, expected, nomorAju]), [["H1", 2, 2, ["001"]], ["H2", 0, 1, ["002"]]]);
  const hawbs = groupAwbs(items.slice(0, 3), { 1: found(2), 2: found(2), 3: found(1) }, { byHawb: true });
  assert.deepEqual(hawbs.map((row) => [row.hawbGroups[0].hawb, row.status]), [
    ["H1", "COMPLETE"], ["H2", "INCOMPLETE"],
  ]);
  assert.equal(other.status, "COMPLETE");
  assert.equal(groupAwbs(items.slice(0, 2), { 1: found(2), 2: found(2) })[0].status, "COMPLETE");
  assert.equal(groupAwbs(items.slice(0, 2), { 1: found(1), 2: found(1) })[0].status, "OVER EXPECTED");
});

test("never guesses missing, failed, invalid or inconsistent manifest quantities", () => {
  const items = [item(1, "H1"), item(2, "H1")];
  assert.equal(groupAwbs(items, { 1: found(1) })[0].status, "LOADING");
  for (const manifests of [
    { 1: found(1), 2: found(2) },
    { 1: found(1), 2: { status: "not_found" } },
    { 1: found(1), 2: found(null) },
    { 1: found(1), 2: found(0) },
    { 1: found(1), 2: found(1.5) },
  ]) {
    const group = groupAwbs(items, manifests)[0];
    assert.equal(group.expected, null);
    assert.equal(group.status, "UNAVAILABLE");
  }
  assert.equal(groupAwbs([item(1, null)], {})[0].status, "UNAVAILABLE");
});
