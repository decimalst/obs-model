import test from "node:test";
import assert from "node:assert/strict";
import { defaults, compileRequest, validateRequest } from "../site/model.mjs";

const good = () => ({
  ...structuredClone(defaults),
  zones: ["us-east-1a", "us-east-1b", "us-east-1c"],
  adminRole: "arn:aws:iam::123456789012:role/Admin",
});

test("startup defaults and partial imports compile into complete resumable tuning", () => {
  for (const selected of [
    {},
    { attempts: 1 },
    { delaySeconds: 30 },
    { timeoutSeconds: 60 },
    { attempts: 360, delaySeconds: 1, timeoutSeconds: 5 },
  ]) {
    const request = { ...good(), startupGates: selected };
    const before = structuredClone(request);
    const expected = { ...defaults.startupGates, ...selected };
    const validated = validateRequest(request);
    const files = compileRequest(request);
    assert.deepEqual(request, before, "validation must not mutate imports");
    assert.deepEqual(validated.startupGates, expected);
    assert.notEqual(validated.startupGates, selected);
    assert.deepEqual(JSON.parse(files["tuning.json"]).startupGates, expected);
    const resumed = JSON.parse(files["deployment-request.json"]);
    assert.deepEqual(resumed.startupGates, expected);
    assert.deepEqual(compileRequest(resumed), files);
    assert.match(
      files["README.md"],
      new RegExp(`${expected.attempts} attempts`),
    );
    assert.match(
      files["README.md"],
      /Kubernetes may restart the init container/,
    );
    assert.match(files["README.md"], /not a total deployment deadline/);
  }
});

test("invalid startup sections and bounds are rejected without changing the import", () => {
  for (const startupGates of [
    null,
    [],
    false,
    "defaults",
    { enabled: false },
    { password: "SYNTHETIC-MARKER" },
    { attempts: 0 },
    { attempts: 361 },
    { attempts: 1.5 },
    { attempts: true },
    { attempts: "120" },
    { delaySeconds: 0 },
    { delaySeconds: 31 },
    { timeoutSeconds: 4 },
    { timeoutSeconds: 61 },
    { timeoutSeconds: null },
  ]) {
    const request = { ...good(), startupGates };
    const before = structuredClone(request);
    assert.throws(() => validateRequest(request), /[Ss]tartup/);
    assert.throws(() => compileRequest(request), /[Ss]tartup/);
    assert.deepEqual(request, before);
  }
});
