// Replays the Python reference's golden traces (tests/golden/*.json) through the TypeScript port.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { Mulberry32, fnv1a, makeConfig, replay, TrafficLab } from "../src/engine/lab.ts";

const dir = join(import.meta.dirname, "..", "..", "tests", "golden");
const files = readdirSync(dir).filter((f) => f.endsWith(".json"));

test("golden files exist", () => assert.ok(files.length >= 4));

test("PRNG and hash match the Python reference", () => {
  assert.equal(new Mulberry32(1).next(), 0.6270739405881613);
  assert.equal(fnv1a(""), 0x811c9dc5);
  assert.equal(fnv1a("a"), 0xe40c292c);
});

for (const f of files) {
  test(`parity: ${f}`, async () => {
    const g = JSON.parse(readFileSync(join(dir, f), "utf8"));
    const lab = replay(makeConfig(g.config), g.arrivals, g.actions);
    assert.deepEqual(lab.compact(), g.compact);
    assert.equal(await lab.digest(), g.digest);
    const s = lab.summary();
    assert.deepEqual(s.outcomes, g.summary.outcomes);
    assert.equal(s.retries_used, g.summary.retries_used);
    assert.equal(s.retries_denied, g.summary.retries_denied);
    assert.equal(s.p95_ms, g.summary.p95_ms);
    assert.deepEqual(s.backends, g.summary.backends);
  });
}

test("incremental stepping equals one-shot replay", async () => {
  const g = JSON.parse(readFileSync(join(dir, "kill-and-recover.json"), "utf8"));
  const lab = new TrafficLab(makeConfig(g.config));
  lab.add_arrivals(g.arrivals);
  let ai = 0;
  for (let t = 0; t < 8000; t += 16.7) {
    while (ai < g.actions.length && g.actions[ai].t <= t) {
      lab.run_until(g.actions[ai].t);
      lab.apply(g.actions[ai]);
      ai++;
    }
    lab.run_until(t);
  }
  lab.run_all();
  assert.equal(await lab.digest(), g.digest);
});

test("validation rejects unsafe input", () => {
  assert.throws(() => makeConfig({ backends: [] }));
  assert.throws(() => makeConfig({ backends: [{ id: "A" }], max_attempts: 9 }));
  const lab = new TrafficLab(makeConfig({ backends: [{ id: "A" }] }));
  assert.throws(() => lab.apply({ t: 0, op: "backend", id: "A", patch: { error_rate: 5 } }));
});
