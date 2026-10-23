// Holds one lab run: the engine, everything the visitor did (for Python replay) and a clock.
import { makeConfig, replay, TrafficLab, Mulberry32, LIMITS, type Action, type Arrival, type LabConfig } from "./engine/lab.ts";

export interface Policy {
  max_attempts: number;
  timeout_ms: number;
  breaker_threshold: number;
  breaker_recovery_ms: number;
  retry_budget_ratio: number;
  canary_percent: number;
}

export const DEFAULT_POLICY: Policy = {
  max_attempts: 3,
  timeout_ms: 250,
  breaker_threshold: 5,
  breaker_recovery_ms: 3000,
  retry_budget_ratio: 0.2,
  canary_percent: 15,
};

export function buildConfig(p: Policy): LabConfig {
  return makeConfig({
    seed: 20261022,
    backends: [
      { id: "A", weight: 2, latency_ms: 38, jitter_ms: 24 },
      { id: "B", weight: 1, latency_ms: 46, jitter_ms: 30 },
      { id: "C", weight: 1, latency_ms: 52, jitter_ms: 36, error_rate: 0.02 },
      { id: "K", pool: "canary", weight: 1, latency_ms: 44, jitter_ms: 28, error_rate: 0.05 },
    ],
    ...p,
    retry_budget_min: 3,
    backoff_base_ms: 30,
    backoff_cap_ms: 400,
    rate_capacity: 20,
    rate_refill_per_s: 10,
  });
}

type DOmit<T, K extends PropertyKey> = T extends unknown ? Omit<T, K> : never;

export const CLOCK_LIMIT = 590000;

export class Session {
  config: LabConfig;
  lab: TrafficLab;
  arrivals: Arrival[] = [];
  actions: Action[] = [];
  vt = 0;
  nextId = 1;
  gen: Mulberry32;
  version = 0;
  constructor(config: LabConfig) {
    this.config = config;
    this.lab = new TrafficLab(config);
    this.gen = new Mulberry32(config.seed ^ 0x5bd1e995);
  }
  get arrivalsLeft() {
    return LIMITS.arrivals - this.arrivals.length;
  }
  get actionsLeft() {
    return LIMITS.actions - this.actions.length;
  }
  advance(t: number) {
    this.vt = Math.min(t, CLOCK_LIMIT);
    this.lab.run_until(this.vt);
  }
  dispatch(arrivals: Omit<Arrival, "id">[]): number {
    const room = Math.min(this.arrivalsLeft, arrivals.length);
    if (room <= 0 || this.vt + 5000 > CLOCK_LIMIT) return 0;
    const batch = arrivals.slice(0, room).map((a) => ({ ...a, id: this.nextId++ }));
    this.lab.add_arrivals(batch);
    this.arrivals.push(...batch);
    this.version++;
    return batch.length;
  }
  act(action: DOmit<Action, "t">): boolean {
    if (this.actionsLeft <= 0) return false;
    const full = { ...action, t: this.vt } as Action;
    this.lab.run_until(this.vt);
    this.lab.apply(full);
    this.actions.push(full);
    this.version++;
    return true;
  }
  /** One-shot replay of everything recorded, used for the cross-check. */
  replayAll() {
    return replay(this.config, this.arrivals, this.actions);
  }
}

export type PatternId = "steady" | "rush" | "noisy" | "wrong";
export const PATTERNS: { id: PatternId; label: string; detail: string }[] = [
  { id: "steady", label: "Steady service", detail: "100 requests over 4 s from 8 clients" },
  { id: "rush", label: "Rush hour", detail: "150 requests inside 300 ms from 12 clients" },
  { id: "noisy", label: "One noisy client", detail: "60 requests in 1 s from one client, 20 from others" },
  { id: "wrong", label: "Wrong platform", detail: "Mix of /api and /apiary paths" },
];

const client = (n: number) => `client-${String(n).padStart(2, "0")}`;
const PATHS = ["/api/orders", "/api/search", "/api/cart", "/api/orders/42"];

export function makePattern(s: Session, id: PatternId): Omit<Arrival, "id">[] {
  const g = s.gen;
  const t0 = s.vt + 1;
  const out: Omit<Arrival, "id">[] = [];
  const path = () => PATHS[Math.floor(g.next() * PATHS.length)];
  if (id === "steady") {
    let t = t0;
    for (let i = 0; i < 100; i++) {
      t += 20 + g.next() * 20;
      out.push({ t, client: client(i % 8), path: path() });
    }
  } else if (id === "rush") {
    for (let i = 0; i < 150; i++) out.push({ t: t0 + g.next() * 300, client: client(Math.floor(g.next() * 12)), path: path() });
  } else if (id === "noisy") {
    for (let i = 0; i < 60; i++) out.push({ t: t0 + g.next() * 1000, client: "client-noisy", path: path() });
    for (let i = 0; i < 20; i++) out.push({ t: t0 + g.next() * 1000, client: client(i % 6), path: path() });
  } else {
    for (let i = 0; i < 24; i++) out.push({ t: t0 + i * 40, client: client(i % 5), path: i % 3 === 0 ? "/apiary" : path() });
  }
  return out.sort((a, b) => a.t - b.t);
}
