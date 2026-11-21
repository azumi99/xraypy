import { test } from "node:test";
import { strict as assert } from "node:assert";
import { warehousePath } from "./routes.js";

test("routes follow the authenticated warehouse", () => {
  assert.equal(warehousePath("/", "cas"), "/cas/dashboard");
  assert.equal(warehousePath("/dashboard", "cas"), "/cas/dashboard");
  assert.equal(warehousePath("/bgd/awb/12", "cas"), "/cas/awb/12");
  assert.equal(warehousePath("/bgd/live", "bgd"), "/bgd/live");
  assert.equal(warehousePath("/cas", "bgd"), "/bgd/dashboard");
});
