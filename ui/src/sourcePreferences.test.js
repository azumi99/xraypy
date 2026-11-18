import { test } from "node:test";
import assert from "node:assert/strict";
import { parseSourcePreference, readSourcePreference, writeSourcePreference } from "./sourcePreferences.js";

test("source preferences validate stored data", () => {
  assert.deepEqual(parseSourcePreference('{"kind":"browser","deviceId":"capture"}'), { kind: "browser", deviceId: "capture" });
  assert.deepEqual(parseSourcePreference('{"kind":"camera:12"}'), { kind: "camera:12" });
  for (const raw of ["not json", "null", '{"kind":"browser","deviceId":""}', '{"kind":"camera:-1"}', '{"kind":"synthetic"}']) assert.equal(parseSourcePreference(raw), null);
});

test("preferences are scoped by warehouse and view and tolerate blocked storage", () => {
  const data = new Map();
  globalThis.localStorage = { getItem: (key) => data.get(key), setItem: (key, value) => data.set(key, value) };
  writeSourcePreference("cas", "top", { kind: "browser", deviceId: "one" });
  writeSourcePreference("cas", "side", { kind: "screen" });
  writeSourcePreference("bgd", "top", { kind: "camera:1" });
  writeSourcePreference("other", "top", { kind: "screen" });
  assert.equal(data.size, 3);
  assert.deepEqual(readSourcePreference("cas", "top"), { kind: "browser", deviceId: "one" });
  assert.deepEqual(readSourcePreference("cas", "side"), { kind: "screen" });
  assert.deepEqual(readSourcePreference("bgd", "top"), { kind: "camera:1" });
  assert.equal(readSourcePreference("bgd", "side"), null);
  globalThis.localStorage = { getItem() { throw Error(); }, setItem() { throw Error(); } };
  assert.equal(readSourcePreference("cas", "top"), null);
  assert.doesNotThrow(() => writeSourcePreference("cas", "top", { kind: "screen" }));
  delete globalThis.localStorage;
});
