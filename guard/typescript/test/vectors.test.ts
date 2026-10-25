// The shared test vectors (guard/test-vectors), run by the TypeScript SDK. The Python SDK runs
// the same files (guard/tests/test_vectors.py); both must give the same answers and hashes.
import { strict as assert } from "node:assert";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import {
  CanonicalError,
  DecisionLog,
  Guard,
  GuardAskUser,
  GuardBlocked,
  PolicyError,
  canonical,
  digest,
  guardTool,
  parsePolicy,
  verifyChain,
} from "../src/index.js";
import type { Check, DecisionBackend } from "../src/index.js";

const VECTORS = new URL("../../../test-vectors/", import.meta.url);
const load = (name: string): any => JSON.parse(readFileSync(new URL(name, VECTORS), "utf8"));

test("canonical JSON and hashes match the Python SDK", () => {
  const data = load("canonical.json");
  assert.ok(data.cases.length >= 20);
  for (const item of data.cases) {
    assert.equal(canonical(item.value), item.canonical, item.name);
    assert.equal(digest(item.value), item.sha256, item.name);
  }
  for (const item of data.invalid) assert.equal(item.python_only, true, item.name);
});

test("NaN, infinity, a lone surrogate and undefined have no canonical form", () => {
  for (const value of [Number.NaN, Number.POSITIVE_INFINITY, "\uD800", { a: undefined }, [() => 1]]) {
    assert.throws(() => canonical(value), CanonicalError);
  }
});

test("valid policies hash as in Python; invalid ones are refused", () => {
  const data = load("policies.json");
  for (const item of data.valid) assert.equal(parsePolicy(item.policy).hash, item.hash, item.name);
  for (const item of data.invalid) assert.throws(() => parsePolicy(item.policy), PolicyError, item.name);
});

function backendFor(spec: any, timeoutS: number): DecisionBackend | null {
  if (spec === null || spec === undefined) return null;
  return {
    name: "vectors-backend",
    async decide() {
      if (spec.raises) throw new Error("the backend broke");
      if (spec.late) {
        await new Promise((resolve) => setTimeout(resolve, timeoutS * 4000));
        return spec.late;
      }
      if (spec.invalid) return structuredClone(spec.invalid);
      return spec.result;
    },
  };
}

test("every layered decision matches the Python SDK", async () => {
  const data = load("decisions.json");
  assert.ok(data.cases.length >= 40);
  for (const item of data.cases) {
    const policy = parsePolicy(item.policy);
    const fires = new Set<string>(item.fires);
    const checks: Record<string, Check> = {};
    for (const [id, decision] of Object.entries<string>(data.checks)) {
      checks[id] = { decision: decision as Check["decision"], check: () => fires.has(id) };
    }
    const log = new DecisionLog();
    const guard = new Guard(policy, {
      backend: backendFor(item.backend, policy.model_check.timeout_s),
      checks,
      log,
      delegation: item.delegation,
      packLimits: item.pack_limits,
    });
    for (const entry of item.history) guard.ledger.add(entry.tool, entry.amount, new Date(entry.sim_time));
    const outcome = await guard.decide(item.request, item.fields);
    const want = item.expected;
    assert.equal(outcome.decision, want.decision, `${item.name}: decision`);
    assert.equal(outcome.layer, want.layer, `${item.name}: layer`);
    assert.deepEqual(outcome.reason_codes, want.reason_codes, `${item.name}: reason codes`);
    assert.equal(outcome.confidence, want.confidence, `${item.name}: confidence`);
    assert.equal(outcome.record?.inputs, want.inputs_hash, `${item.name}: inputs hash`);
    assert.equal(outcome.record?.policy.hash, want.policy_hash, `${item.name}: policy hash`);
    assert.equal(outcome.record?.policy.delegation, want.delegation_hash, `${item.name}: delegation hash`);
    assert.deepEqual(verifyChain(log.records), [], `${item.name}: chain`);
  }
});

test("chain verification finds every edited, removed or reordered record", () => {
  const data = load("chains.json");
  for (const item of data.cases) {
    const problems = verifyChain(item.records, { head: item.head, count: item.count });
    assert.equal(problems.length === 0, item.ok, `${item.name}: ${problems.join("; ")}`);
  }
});

test("the wrapper runs only what the guard or the user allowed", async () => {
  const policy = parsePolicy({
    format: "guard-policy-v1",
    id: "wrap",
    version: 1,
    actions: { pay: { budget: { per_action: 1000 }, confirm_above: 500 } },
  });
  const guard = new Guard(policy, { log: new DecisionLog() });
  const paid: number[] = [];
  const options = {
    tool: "pay",
    world: "vectors",
    instruction: "Pay the shop.",
    now: () => "2026-01-01T09:00:00Z",
    fields: (args: { amount: number }) => ({ amount: args.amount }),
  };
  const pay = guardTool(guard, (args: { amount: number }) => paid.push(args.amount), options);
  await pay({ amount: 100 });
  await assert.rejects(pay({ amount: 5000 }), GuardBlocked);
  await assert.rejects(pay({ amount: 700 }), GuardAskUser);
  const confirmed = guardTool(guard, (args: { amount: number }) => paid.push(args.amount), {
    ...options,
    confirm: () => true,
  });
  await confirmed({ amount: 700 });
  assert.deepEqual(paid, [100, 700]);
  assert.deepEqual(
    guard.log?.records.map((record) => `${record.decision}/${record.layer}`),
    ["allow/hard", "block/hard", "ask_user/hard", "ask_user/hard", "allow/user"],
  );
  assert.deepEqual(verifyChain(guard.log?.records ?? []), []);
});

test("a log written to a file verifies after reading it back", async () => {
  const path = join(mkdtempSync(join(tmpdir(), "guard-")), "decisions.jsonl");
  const policy = parsePolicy({ format: "guard-policy-v1", id: "file", version: 1, actions: { pay: {} } });
  const guard = new Guard(policy, { log: new DecisionLog(path) });
  for (const minute of ["00", "01", "02"]) {
    await guard.decide({
      world: "vectors",
      instruction: "x",
      action: { tool: "pay", args: { n: minute } },
      sim_time: `2026-01-01T09:${minute}:00Z`,
    });
  }
  const rows = readFileSync(path, "utf8").trim().split("\n").map((line) => JSON.parse(line));
  const head = guard.log?.head;
  assert.deepEqual(verifyChain(rows, { head: head?.hash ?? null, count: head?.count ?? null }), []);
  rows[1].confidence = 0.5;
  assert.notDeepEqual(verifyChain(rows), []);
});

test("a policy naming a check nobody provides is refused", () => {
  const policy = parsePolicy({
    format: "guard-policy-v1",
    id: "checks",
    version: 1,
    actions: { pay: { checks: ["vectors.rule.missing"] } },
  });
  assert.throws(() => new Guard(policy), PolicyError);
});
