// Line-for-line TypeScript port of src/traffic_lab.py (the Python reference).
// tests/parity.test.ts replays the Python golden traces and requires identical results.
// Only + - * / and floor are used on floats so both languages agree bit-for-bit.

export class Mulberry32 {
  state: number;
  constructor(seed: number) {
    this.state = seed >>> 0;
  }
  next(): number {
    this.state = (this.state + 0x6d2b79f5) >>> 0;
    let t = this.state;
    t = Math.imul(t ^ (t >>> 15), t | 1) >>> 0;
    t = (t ^ ((t + (Math.imul(t ^ (t >>> 7), t | 61) >>> 0)) >>> 0)) >>> 0;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  }
}

export function fnv1a(text: string): number {
  let h = 0x811c9dc5;
  for (const byte of new TextEncoder().encode(text)) {
    h ^= byte;
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}

export const LIMITS = { backends: 6, arrivals: 2000, actions: 60 };

export type Pool = "stable" | "canary";
export interface BackendSpec {
  id: string;
  pool: Pool;
  weight: number;
  latency_ms: number;
  jitter_ms: number;
  error_rate: number;
  down: boolean;
}
export interface LabConfig {
  backends: BackendSpec[];
  seed: number;
  canary_percent: number;
  timeout_ms: number;
  max_attempts: number;
  backoff_base_ms: number;
  backoff_cap_ms: number;
  retry_budget_ratio: number;
  retry_budget_min: number;
  breaker_threshold: number;
  breaker_window_ms: number;
  breaker_recovery_ms: number;
  rate_capacity: number;
  rate_refill_per_s: number;
  connect_ms: number;
  route_prefix: string;
}

const BACKEND_DEFAULTS: Omit<BackendSpec, "id"> = {
  pool: "stable", weight: 1, latency_ms: 40, jitter_ms: 20, error_rate: 0, down: false,
};
export const CONFIG_DEFAULTS: Omit<LabConfig, "backends"> = {
  seed: 7, canary_percent: 0, timeout_ms: 250, max_attempts: 3, backoff_base_ms: 25,
  backoff_cap_ms: 400, retry_budget_ratio: 0.2, retry_budget_min: 3, breaker_threshold: 5,
  breaker_window_ms: 10000, breaker_recovery_ms: 3000, rate_capacity: 20, rate_refill_per_s: 10,
  connect_ms: 3, route_prefix: "/api",
};

type Partialish = Partial<Omit<LabConfig, "backends">> & {
  backends: (Partial<BackendSpec> & { id: string })[];
};

export function makeConfig(d: Partialish): LabConfig {
  const cfg: LabConfig = {
    ...CONFIG_DEFAULTS,
    ...d,
    backends: d.backends.map((b) => ({ ...BACKEND_DEFAULTS, ...b })),
  };
  validate(cfg);
  return cfg;
}

const isInt = (v: unknown) => typeof v === "number" && Number.isInteger(v);

export function validate(c: LabConfig): void {
  if (c.backends.length < 1 || c.backends.length > LIMITS.backends) throw new Error("Use 1-6 backends");
  const ids = c.backends.map((b) => b.id);
  if (new Set(ids).size !== ids.length || ids.some((i) => !i || i.length > 24))
    throw new Error("Backend ids must be unique, 1-24 characters");
  for (const b of c.backends) {
    if (b.pool !== "stable" && b.pool !== "canary") throw new Error("Pool must be stable or canary");
    if (!(isInt(b.weight) && b.weight >= 0 && b.weight <= 10)) throw new Error("Weight must be an integer 0-10");
    if (!(b.latency_ms >= 0 && b.latency_ms <= 5000 && b.jitter_ms >= 0 && b.jitter_ms <= 5000 && b.error_rate >= 0 && b.error_rate <= 1))
      throw new Error("Backend latency/jitter/error rate out of range");
  }
  if (!(c.canary_percent >= 0 && c.canary_percent <= 100)) throw new Error("Canary percent must be 0-100");
  if (!(isInt(c.max_attempts) && c.max_attempts >= 1 && c.max_attempts <= 5)) throw new Error("Max attempts must be 1-5");
  for (const name of ["timeout_ms", "backoff_base_ms", "backoff_cap_ms", "breaker_window_ms", "breaker_recovery_ms", "rate_capacity", "rate_refill_per_s", "connect_ms"] as const) {
    const v = c[name];
    if (!(typeof v === "number" && v > 0 && v <= 600000)) throw new Error(`${name} out of range`);
  }
  if (!(c.retry_budget_ratio >= 0 && c.retry_budget_ratio <= 1 && c.retry_budget_min >= 0 && c.retry_budget_min <= 100))
    throw new Error("Retry budget out of range");
  if (!(isInt(c.breaker_threshold) && c.breaker_threshold >= 1 && c.breaker_threshold <= 1000))
    throw new Error("Breaker threshold must be 1-1000");
}

export type BreakerState = "closed" | "open" | "half_open";
export interface Transition { t: number; from: BreakerState; to: BreakerState }

export class Breaker {
  state: BreakerState = "closed";
  failures: number[] = [];
  opened_at = 0;
  probe_in_flight = false;
  transitions: Transition[] = [];
  threshold: number;
  window_ms: number;
  recovery_ms: number;
  constructor(threshold: number, window_ms: number, recovery_ms: number) {
    this.threshold = threshold;
    this.window_ms = window_ms;
    this.recovery_ms = recovery_ms;
  }
  private move(now: number, to: BreakerState) {
    this.transitions.push({ t: now, from: this.state, to });
    this.state = to;
  }
  can_pass(now: number): boolean {
    if (this.state === "closed") return true;
    if (this.state === "open") {
      if (now - this.opened_at >= this.recovery_ms) {
        this.move(now, "half_open");
        return !this.probe_in_flight;
      }
      return false;
    }
    return !this.probe_in_flight;
  }
  on_success(now: number, was_probe: boolean) {
    if (was_probe) this.probe_in_flight = false;
    if (this.state === "half_open" && was_probe) {
      this.failures = [];
      this.move(now, "closed");
    }
  }
  on_failure(now: number, was_probe: boolean) {
    if (was_probe) this.probe_in_flight = false;
    this.failures.push(now);
    const cutoff = now - this.window_ms;
    this.failures = this.failures.filter((f) => f >= cutoff);
    if (this.state === "half_open" && was_probe) {
      this.opened_at = now;
      this.move(now, "open");
    } else if (this.state === "closed" && this.failures.length >= this.threshold) {
      this.opened_at = now;
      this.move(now, "open");
    }
  }
}

export interface BackendState {
  spec: BackendSpec;
  breaker: Breaker;
  current_weight: number;
  requests: number;
  errors: number;
  in_flight: number;
  peak_in_flight: number;
}

export type AttemptOutcome = "pending" | "ok" | "error" | "timeout" | "refused";
export interface Attempt {
  backend: string;
  start: number;
  end: number;
  outcome: AttemptOutcome;
  probe: boolean;
  backoff_ms: number;
}
export type Outcome = "pending" | "ok" | "failed" | "rate_limited" | "no_route" | "short_circuited";
export interface RequestTrace {
  id: number;
  client: string;
  path: string;
  arrival: number;
  pool: Pool;
  attempts: Attempt[];
  outcome: Outcome;
  status: number;
  finished: number;
  note: string;
}
export interface Arrival { id: number; t: number; client: string; path: string }
export type Action =
  | { t: number; op: "backend"; id: string; patch: Partial<Pick<BackendSpec, "weight" | "latency_ms" | "jitter_ms" | "error_rate" | "down">> }
  | { t: number; op: "canary"; percent: number };

type Kind = "arrive" | "complete" | "retry";
type QItem = [number, number, Kind, number];
const KIND_RANK: Record<Kind, number> = { complete: 0, retry: 1, arrive: 2 };

export class TrafficLab {
  config: LabConfig;
  rng: Mulberry32;
  now = 0;
  seq = 0;
  queue: QItem[] = [];
  backends = new Map<string, BackendState>();
  order: string[];
  buckets = new Map<string, { tokens: number; last: number }>();
  requests = new Map<number, RequestTrace>();
  pending = new Set<number>();
  originals = 0;
  retries_used = 0;
  retries_denied = 0;
  completed: number[] = [];

  constructor(config: LabConfig) {
    validate(config);
    this.config = { ...config, backends: config.backends.map((b) => ({ ...b })) };
    this.rng = new Mulberry32(config.seed);
    for (const spec of this.config.backends) {
      this.backends.set(spec.id, {
        spec: { ...spec },
        breaker: new Breaker(config.breaker_threshold, config.breaker_window_ms, config.breaker_recovery_ms),
        current_weight: 0, requests: 0, errors: 0, in_flight: 0, peak_in_flight: 0,
      });
    }
    this.order = this.config.backends.map((b) => b.id);
  }

  private push(t: number, kind: Kind, rid: number) {
    // Ties are ordered by (kind, request id), never by insertion order, so a run
    // stepped frame-by-frame equals a one-shot replay of the same inputs.
    this.seq += 1;
    const item: QItem = [t, this.seq, kind, rid];
    const kr = KIND_RANK[kind];
    let lo = 0;
    let hi = this.queue.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      const q = this.queue[mid];
      const qr = KIND_RANK[q[2]];
      if (q[0] < t || (q[0] === t && (qr < kr || (qr === kr && q[3] < rid)))) lo = mid + 1;
      else hi = mid;
    }
    this.queue.splice(lo, 0, item);
  }

  add_arrivals(arrivals: Arrival[]) {
    if (this.requests.size + arrivals.length > LIMITS.arrivals) throw new Error("Too many arrivals for one lab run");
    for (const a of arrivals) {
      const rid = a.id;
      const t = a.t;
      if (this.requests.has(rid) || t < this.now || t > 600000) throw new Error("Arrival ids must be unique and times in the future");
      this.requests.set(rid, {
        id: rid, client: String(a.client ?? "anon").slice(0, 32), path: String(a.path ?? this.config.route_prefix).slice(0, 80),
        arrival: t, pool: "stable", attempts: [], outcome: "pending", status: 0, finished: 0, note: "",
      });
      this.push(t, "arrive", rid);
    }
  }

  run_until(t: number) {
    while (this.queue.length && this.queue[0][0] <= t) {
      const [et, , kind, rid] = this.queue.shift()!;
      this.now = et;
      if (kind === "arrive") this.arrive(rid);
      else if (kind === "complete") this.complete(rid);
      else this.attempt(rid);
    }
    if (t > this.now && t !== Infinity) this.now = t;
  }

  run_all() {
    this.run_until(Infinity);
  }

  apply(action: Action) {
    if (action.op === "backend") {
      const b = this.backends.get(action.id);
      if (!b) throw new Error("Unknown backend");
      for (const k of ["weight", "latency_ms", "jitter_ms", "error_rate", "down"] as const) {
        if (k in action.patch) (b.spec as unknown as Record<string, unknown>)[k] = action.patch[k];
      }
      const idx = this.config.backends.findIndex((x) => x.id === action.id);
      this.config.backends[idx] = { ...b.spec };
    } else if (action.op === "canary") {
      this.config.canary_percent = action.percent;
    } else {
      throw new Error("Unknown action");
    }
    validate(this.config);
  }

  private rate_ok(client: string, now: number): boolean {
    const cap = this.config.rate_capacity;
    let b = this.buckets.get(client);
    if (!b) {
      b = { tokens: cap, last: now };
      this.buckets.set(client, b);
    }
    b.tokens = Math.min(cap, b.tokens + ((now - b.last) * this.config.rate_refill_per_s) / 1000);
    b.last = now;
    if (b.tokens >= 1) {
      b.tokens -= 1;
      return true;
    }
    return false;
  }

  private matches(path: string): boolean {
    const p = path.split("?", 1)[0];
    const prefix = this.config.route_prefix.replace(/\/+$/, "");
    return p === prefix || p.startsWith(prefix + "/");
  }

  pick(pool: Pool, exclude: string[], now: number): BackendState | null {
    const members = this.order.map((i) => this.backends.get(i)!).filter((b) => b.spec.pool === pool && b.spec.weight > 0);
    const fresh = members.filter((b) => !exclude.includes(b.spec.id));
    const candidates = (fresh.length ? fresh : members).filter((b) => b.breaker.can_pass(now));
    if (!candidates.length) return null;
    let total = 0;
    let best: BackendState | null = null;
    for (const b of candidates) {
      b.current_weight += b.spec.weight;
      total += b.spec.weight;
      if (best === null || b.current_weight > best.current_weight) best = b;
    }
    best!.current_weight -= total;
    return best;
  }

  private retry_allowed(): boolean {
    const budget = this.config.retry_budget_min + this.config.retry_budget_ratio * this.originals;
    return this.retries_used + 1 <= budget;
  }

  private finish(r: RequestTrace, outcome: Outcome, status: number, now: number, note = "") {
    r.outcome = outcome;
    r.status = status;
    r.finished = now;
    if (note) r.note = note;
    this.pending.delete(r.id);
    this.completed.push(r.id);
  }

  private arrive(rid: number) {
    const r = this.requests.get(rid)!;
    const now = this.now;
    if (!this.rate_ok(r.client, now)) return this.finish(r, "rate_limited", 429, now, "token bucket empty");
    if (!this.matches(r.path)) return this.finish(r, "no_route", 404, now, "no route prefix match");
    this.originals += 1;
    let canaryPool = false;
    for (const b of this.backends.values()) if (b.spec.pool === "canary" && b.spec.weight > 0) canaryPool = true;
    r.pool = canaryPool && fnv1a(r.client) % 100 < this.config.canary_percent ? "canary" : "stable";
    this.pending.add(rid);
    this.attempt(rid);
  }

  private attempt(rid: number) {
    const r = this.requests.get(rid)!;
    const now = this.now;
    const tried = r.attempts.map((a) => a.backend);
    let b = this.pick(r.pool, tried, now);
    if (b === null && r.pool === "canary") {
      b = this.pick("stable", tried, now);
      if (b !== null) r.note = "canary unavailable, fell back to stable";
    }
    if (b === null) {
      if (!r.attempts.length) return this.finish(r, "short_circuited", 503, now, "every breaker open");
      const last = r.attempts[r.attempts.length - 1].outcome;
      return this.finish(r, "failed", last === "timeout" ? 504 : 502, now, "no backend left to retry");
    }
    const probe = b.breaker.state === "half_open";
    if (probe) b.breaker.probe_in_flight = true;
    const spec = b.spec;
    let outcome: AttemptOutcome;
    let elapsed: number;
    if (spec.down) {
      outcome = "refused";
      elapsed = this.config.connect_ms;
    } else {
      const latency = spec.latency_ms + this.rng.next() * spec.jitter_ms;
      if (latency > this.config.timeout_ms) {
        outcome = "timeout";
        elapsed = this.config.timeout_ms;
      } else {
        outcome = this.rng.next() < spec.error_rate ? "error" : "ok";
        elapsed = latency;
      }
    }
    b.requests += 1;
    b.in_flight += 1;
    b.peak_in_flight = Math.max(b.peak_in_flight, b.in_flight);
    r.attempts.push({ backend: spec.id, start: now, end: now + elapsed, outcome, probe, backoff_ms: 0 });
    this.push(now + elapsed, "complete", rid);
  }

  private complete(rid: number) {
    const r = this.requests.get(rid)!;
    const now = this.now;
    const a = r.attempts[r.attempts.length - 1];
    const b = this.backends.get(a.backend)!;
    b.in_flight -= 1;
    if (a.outcome === "ok") {
      b.breaker.on_success(now, a.probe);
      return this.finish(r, "ok", 200, now);
    }
    b.errors += 1;
    b.breaker.on_failure(now, a.probe);
    const status = a.outcome === "timeout" ? 504 : 502;
    if (r.attempts.length >= this.config.max_attempts) return this.finish(r, "failed", status, now, "attempts exhausted");
    if (!this.retry_allowed()) {
      this.retries_denied += 1;
      return this.finish(r, "failed", status, now, "retry budget exhausted");
    }
    this.retries_used += 1;
    const exp = this.config.backoff_base_ms * Math.pow(2, r.attempts.length - 1);
    const ceiling = exp < this.config.backoff_cap_ms ? exp : this.config.backoff_cap_ms;
    a.backoff_ms = this.rng.next() * ceiling;
    this.push(now + a.backoff_ms, "retry", rid);
  }

  summary() {
    const done = this.completed.map((i) => this.requests.get(i)!);
    const ok = done.filter((r) => r.outcome === "ok");
    const lat = ok.map((r) => r.finished - r.arrival).sort((x, y) => x - y);
    const pct = (p: number) => (lat.length ? lat[Math.min(lat.length - 1, Math.floor(p * lat.length))] : 0);
    const counts: Record<string, number> = {};
    for (const r of done) counts[r.outcome] = (counts[r.outcome] ?? 0) + 1;
    return {
      now: this.now,
      completed: done.length,
      pending: this.pending.size,
      outcomes: counts,
      p50_ms: pct(0.5),
      p95_ms: pct(0.95),
      retries_used: this.retries_used,
      retries_denied: this.retries_denied,
      backends: this.order.map((i) => {
        const b = this.backends.get(i)!;
        return {
          id: i, state: b.breaker.state, requests: b.requests, errors: b.errors,
          in_flight: b.in_flight, peak_in_flight: b.peak_in_flight, transitions: [...b.breaker.transitions],
        };
      }),
    };
  }

  compact(): (string | number)[][] {
    return [...this.requests.keys()].sort((a, b) => a - b).map((rid) => {
      const r = this.requests.get(rid)!;
      return [r.id, r.outcome, r.status, r.pool, r.attempts.map((a) => `${a.backend}:${a.outcome}`).join(">"),
        r.outcome !== "pending" ? Math.trunc(r.finished * 1000) : -1];
    });
  }

  async digest(): Promise<string> {
    const text = this.compact().map((row) => row.join("|")).join("\n");
    const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
    return [...new Uint8Array(buf)].map((x) => x.toString(16).padStart(2, "0")).join("");
  }
}

export function replay(config: LabConfig, arrivals: Arrival[], actions: Action[]): TrafficLab {
  if (actions.length > LIMITS.actions) throw new Error("Too many actions");
  const lab = new TrafficLab(config);
  lab.add_arrivals(arrivals);
  for (const act of actions) {
    if (act.t < lab.now) throw new Error("Actions must be in time order");
    lab.run_until(act.t);
    lab.apply(act);
  }
  lab.run_all();
  return lab;
}
