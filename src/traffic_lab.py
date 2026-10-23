"""
Traffic lab: a deterministic, event-driven model of the gateway's routing policies.

It is the reference implementation behind the public website. The browser runs a
line-for-line TypeScript port (site/src/engine/lab.ts); tests/test_traffic_lab.py
exports golden traces that the TypeScript tests must reproduce exactly.

Policies implemented here (all on a virtual millisecond clock, no sockets):
  * smooth weighted round-robin (nginx algorithm) per backend pool
  * canary split: a stable hash of the client id picks the canary pool
  * per-backend circuit breaker (closed -> open -> half-open single probe)
  * retries with capped exponential backoff and full jitter, a per-try timeout,
    "try a different backend" on retry, and a retry budget (min + ratio)
  * per-client token-bucket rate limiting
  * a trace (span list) for every request

Concurrency is real in the model: arrivals and attempt completions are events in a
time-ordered queue, so requests overlap, a half-open probe blocks other traffic
until it completes, and a backend killed mid-burst affects in-flight work.
Backends are simulated (latency, jitter, error rate, down); no network is used.
Only + - * / and floor are used on floats so Python and JavaScript agree bit-for-bit.
"""
from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional

MASK = 0xFFFFFFFF


def _imul(a: int, b: int) -> int:
    return (a * b) & MASK


class Mulberry32:
    """Small seeded PRNG with an identical JavaScript implementation."""

    def __init__(self, seed: int):
        self.state = seed & MASK

    def next(self) -> float:
        self.state = (self.state + 0x6D2B79F5) & MASK
        t = self.state
        t = _imul(t ^ (t >> 15), t | 1)
        t = (t ^ ((t + _imul(t ^ (t >> 7), t | 61)) & MASK)) & MASK
        return ((t ^ (t >> 14)) & MASK) / 4294967296


def fnv1a(text: str) -> int:
    h = 0x811C9DC5
    for byte in text.encode("utf-8"):
        h ^= byte
        h = _imul(h, 0x01000193)
    return h


# ── Configuration ────────────────────────────────────────────────────────────

LIMITS = {"backends": 6, "arrivals": 2000, "actions": 60}
KIND_RANK = {"complete": 0, "retry": 1, "arrive": 2}


@dataclass
class BackendSpec:
    id: str
    pool: str = "stable"          # "stable" | "canary"
    weight: int = 1               # 0 drains the backend
    latency_ms: float = 40.0      # base latency
    jitter_ms: float = 20.0       # uniform extra latency in [0, jitter)
    error_rate: float = 0.0       # probability of a 5xx once a response arrives
    down: bool = False            # connection refused after connect_ms


@dataclass
class LabConfig:
    backends: List[BackendSpec]
    seed: int = 7
    canary_percent: float = 0.0
    timeout_ms: float = 250.0
    max_attempts: int = 3
    backoff_base_ms: float = 25.0
    backoff_cap_ms: float = 400.0
    retry_budget_ratio: float = 0.2
    retry_budget_min: int = 3
    breaker_threshold: int = 5
    breaker_window_ms: float = 10000.0
    breaker_recovery_ms: float = 3000.0
    rate_capacity: float = 20.0
    rate_refill_per_s: float = 10.0
    connect_ms: float = 3.0
    route_prefix: str = "/api"

    @staticmethod
    def from_dict(d: dict) -> "LabConfig":
        allowed = set(LabConfig.__dataclass_fields__) - {"backends"}
        kwargs = {k: v for k, v in d.items() if k in allowed}
        backends = [BackendSpec(**{k: v for k, v in b.items() if k in BackendSpec.__dataclass_fields__})
                    for b in d.get("backends", [])]
        cfg = LabConfig(backends=backends, **kwargs)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not 1 <= len(self.backends) <= LIMITS["backends"]:
            raise ValueError("Use 1-6 backends")
        ids = [b.id for b in self.backends]
        if len(set(ids)) != len(ids) or any(not i or len(i) > 24 for i in ids):
            raise ValueError("Backend ids must be unique, 1-24 characters")
        for b in self.backends:
            if b.pool not in ("stable", "canary"):
                raise ValueError("Pool must be stable or canary")
            if not (isinstance(b.weight, int) and 0 <= b.weight <= 10):
                raise ValueError("Weight must be an integer 0-10")
            if not (0 <= b.latency_ms <= 5000 and 0 <= b.jitter_ms <= 5000 and 0 <= b.error_rate <= 1):
                raise ValueError("Backend latency/jitter/error rate out of range")
        if not (0 <= self.canary_percent <= 100):
            raise ValueError("Canary percent must be 0-100")
        if not (isinstance(self.max_attempts, int) and 1 <= self.max_attempts <= 5):
            raise ValueError("Max attempts must be 1-5")
        for name in ("timeout_ms", "backoff_base_ms", "backoff_cap_ms", "breaker_window_ms",
                     "breaker_recovery_ms", "rate_capacity", "rate_refill_per_s", "connect_ms"):
            v = getattr(self, name)
            if not (isinstance(v, (int, float)) and 0 < v <= 600000):
                raise ValueError(f"{name} out of range")
        if not (0 <= self.retry_budget_ratio <= 1 and 0 <= self.retry_budget_min <= 100):
            raise ValueError("Retry budget out of range")
        if not (isinstance(self.breaker_threshold, int) and 1 <= self.breaker_threshold <= 1000):
            raise ValueError("Breaker threshold must be 1-1000")


# ── Runtime pieces ───────────────────────────────────────────────────────────

class Breaker:
    def __init__(self, threshold: int, window_ms: float, recovery_ms: float):
        self.threshold = threshold
        self.window_ms = window_ms
        self.recovery_ms = recovery_ms
        self.state = "closed"
        self.failures: List[float] = []
        self.opened_at = 0.0
        self.probe_in_flight = False
        self.transitions: List[dict] = []

    def _move(self, now: float, to: str) -> None:
        self.transitions.append({"t": now, "from": self.state, "to": to})
        self.state = to

    def can_pass(self, now: float) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            if now - self.opened_at >= self.recovery_ms:
                self._move(now, "half_open")
                return not self.probe_in_flight
            return False
        return not self.probe_in_flight

    def on_success(self, now: float, was_probe: bool) -> None:
        if was_probe:
            self.probe_in_flight = False
        if self.state == "half_open" and was_probe:
            self.failures = []
            self._move(now, "closed")

    def on_failure(self, now: float, was_probe: bool) -> None:
        if was_probe:
            self.probe_in_flight = False
        self.failures.append(now)
        cutoff = now - self.window_ms
        self.failures = [f for f in self.failures if f >= cutoff]
        if self.state == "half_open" and was_probe:
            self.opened_at = now
            self._move(now, "open")
        elif self.state == "closed" and len(self.failures) >= self.threshold:
            self.opened_at = now
            self._move(now, "open")


@dataclass
class BackendState:
    spec: BackendSpec
    breaker: Breaker
    current_weight: int = 0
    requests: int = 0
    errors: int = 0
    in_flight: int = 0
    peak_in_flight: int = 0


@dataclass
class Bucket:
    tokens: float
    last: float


@dataclass
class Attempt:
    backend: str
    start: float
    end: float = 0.0
    outcome: str = "pending"     # ok | error | timeout | refused
    probe: bool = False
    backoff_ms: float = 0.0


@dataclass
class RequestTrace:
    id: int
    client: str
    path: str
    arrival: float
    pool: str = "stable"
    attempts: List[Attempt] = field(default_factory=list)
    outcome: str = "pending"     # ok | failed | rate_limited | no_route | short_circuited
    status: int = 0
    finished: float = 0.0
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "client": self.client, "path": self.path, "arrival": self.arrival,
            "pool": self.pool, "outcome": self.outcome, "status": self.status,
            "finished": self.finished, "note": self.note,
            "attempts": [a.__dict__.copy() for a in self.attempts],
        }


class TrafficLab:
    """Event-driven gateway model. Call add_arrivals(), run_until(t), apply()."""

    def __init__(self, config: LabConfig):
        config.validate()
        self.config = copy.deepcopy(config)
        self.rng = Mulberry32(config.seed)
        self.now = 0.0
        self.seq = 0
        self.queue: List[tuple] = []          # (time, seq, kind, request_id)
        self.backends: Dict[str, BackendState] = {}
        for spec in config.backends:
            self.backends[spec.id] = BackendState(
                spec=BackendSpec(**spec.__dict__),
                breaker=Breaker(config.breaker_threshold, config.breaker_window_ms, config.breaker_recovery_ms))
        self.order = [s.id for s in config.backends]
        self.buckets: Dict[str, Bucket] = {}
        self.requests: Dict[int, RequestTrace] = {}
        self.pending: Dict[int, dict] = {}
        self.originals = 0
        self.retries_used = 0
        self.retries_denied = 0
        self.completed: List[int] = []

    # queue ------------------------------------------------------------------
    def _push(self, t: float, kind: str, rid: int) -> None:
        # Ties are ordered by (kind, request id), never by insertion order, so a run
        # stepped frame-by-frame equals a one-shot replay of the same inputs.
        self.seq += 1
        key = (t, KIND_RANK[kind], rid)
        lo, hi = 0, len(self.queue)
        while lo < hi:
            mid = (lo + hi) // 2
            q = self.queue[mid]
            if (q[0], KIND_RANK[q[2]], q[3]) < key:
                lo = mid + 1
            else:
                hi = mid
        self.queue.insert(lo, (t, self.seq, kind, rid))

    def add_arrivals(self, arrivals: List[dict]) -> None:
        if len(self.requests) + len(arrivals) > LIMITS["arrivals"]:
            raise ValueError("Too many arrivals for one lab run")
        for a in arrivals:
            rid = int(a["id"])
            t = float(a["t"])
            if rid in self.requests or t < self.now or t > 600000:
                raise ValueError("Arrival ids must be unique and times in the future")
            client = str(a.get("client", "anon"))[:32]
            path = str(a.get("path", self.config.route_prefix))[:80]
            self.requests[rid] = RequestTrace(id=rid, client=client, path=path, arrival=t)
            self._push(t, "arrive", rid)

    def run_until(self, t: float) -> None:
        while self.queue and self.queue[0][0] <= t:
            et, _, kind, rid = self.queue.pop(0)
            self.now = et
            if kind == "arrive":
                self._arrive(rid)
            elif kind == "complete":
                self._complete(rid)
            else:
                self._attempt(rid)
        if t > self.now and t != float("inf"):
            self.now = t

    def run_all(self) -> None:
        self.run_until(float("inf"))

    def apply(self, action: dict) -> None:
        """Visitor action at the current virtual time: patch a backend or the canary split."""
        op = action.get("op")
        if op == "backend":
            b = self.backends.get(action.get("id"))
            if b is None:
                raise ValueError("Unknown backend")
            patch = {k: v for k, v in action.get("patch", {}).items()
                     if k in ("weight", "latency_ms", "jitter_ms", "error_rate", "down")}
            for k, v in patch.items():
                setattr(b.spec, k, v)
            idx = self.order.index(b.spec.id)
            self.config.backends[idx] = BackendSpec(**b.spec.__dict__)
        elif op == "canary":
            self.config.canary_percent = float(action["percent"])
        else:
            raise ValueError("Unknown action")
        self.config.validate()

    # policies ---------------------------------------------------------------
    def _rate_ok(self, client: str, now: float) -> bool:
        cap = self.config.rate_capacity
        b = self.buckets.get(client)
        if b is None:
            b = Bucket(tokens=cap, last=now)
            self.buckets[client] = b
        b.tokens = min(cap, b.tokens + (now - b.last) * self.config.rate_refill_per_s / 1000)
        b.last = now
        if b.tokens >= 1:
            b.tokens -= 1
            return True
        return False

    def _matches(self, path: str) -> bool:
        p = path.split("?", 1)[0]
        prefix = self.config.route_prefix.rstrip("/")
        return p == prefix or p.startswith(prefix + "/")

    def _pick(self, pool: str, exclude: List[str], now: float) -> Optional[BackendState]:
        members = [self.backends[i] for i in self.order
                   if self.backends[i].spec.pool == pool and self.backends[i].spec.weight > 0]
        fresh = [b for b in members if b.spec.id not in exclude]
        candidates = [b for b in (fresh or members) if b.breaker.can_pass(now)]
        if not candidates:
            return None
        total = 0
        best: Optional[BackendState] = None
        for b in candidates:
            b.current_weight += b.spec.weight
            total += b.spec.weight
            if best is None or b.current_weight > best.current_weight:
                best = b
        best.current_weight -= total
        return best

    def _retry_allowed(self) -> bool:
        budget = self.config.retry_budget_min + self.config.retry_budget_ratio * self.originals
        return self.retries_used + 1 <= budget

    def _finish(self, r: RequestTrace, outcome: str, status: int, now: float, note: str = "") -> None:
        r.outcome, r.status, r.finished = outcome, status, now
        if note:
            r.note = note
        self.pending.pop(r.id, None)
        self.completed.append(r.id)

    # events -----------------------------------------------------------------
    def _arrive(self, rid: int) -> None:
        r = self.requests[rid]
        now = self.now
        if not self._rate_ok(r.client, now):
            return self._finish(r, "rate_limited", 429, now, "token bucket empty")
        if not self._matches(r.path):
            return self._finish(r, "no_route", 404, now, "no route prefix match")
        self.originals += 1
        canary_pool = any(b.spec.pool == "canary" and b.spec.weight > 0 for b in self.backends.values())
        r.pool = "canary" if canary_pool and (fnv1a(r.client) % 100) < self.config.canary_percent else "stable"
        self.pending[rid] = {}
        self._attempt(rid)

    def _attempt(self, rid: int) -> None:
        r = self.requests[rid]
        now = self.now
        tried = [a.backend for a in r.attempts]
        b = self._pick(r.pool, tried, now)
        if b is None and r.pool == "canary":
            b = self._pick("stable", tried, now)
            if b is not None:
                r.note = "canary unavailable, fell back to stable"
        if b is None:
            if not r.attempts:
                return self._finish(r, "short_circuited", 503, now, "every breaker open")
            last = r.attempts[-1].outcome
            return self._finish(r, "failed", 504 if last == "timeout" else 502, now, "no backend left to retry")
        probe = b.breaker.state == "half_open"
        if probe:
            b.breaker.probe_in_flight = True
        spec = b.spec
        if spec.down:
            outcome, elapsed = "refused", self.config.connect_ms
        else:
            latency = spec.latency_ms + self.rng.next() * spec.jitter_ms
            if latency > self.config.timeout_ms:
                outcome, elapsed = "timeout", self.config.timeout_ms
            else:
                outcome = "error" if self.rng.next() < spec.error_rate else "ok"
                elapsed = latency
        b.requests += 1
        b.in_flight += 1
        b.peak_in_flight = max(b.peak_in_flight, b.in_flight)
        r.attempts.append(Attempt(backend=spec.id, start=now, end=now + elapsed, outcome=outcome, probe=probe))
        self._push(now + elapsed, "complete", rid)

    def _complete(self, rid: int) -> None:
        r = self.requests[rid]
        now = self.now
        a = r.attempts[-1]
        b = self.backends[a.backend]
        b.in_flight -= 1
        if a.outcome == "ok":
            b.breaker.on_success(now, a.probe)
            return self._finish(r, "ok", 200, now)
        b.errors += 1
        b.breaker.on_failure(now, a.probe)
        status = 504 if a.outcome == "timeout" else 502
        if len(r.attempts) >= self.config.max_attempts:
            return self._finish(r, "failed", status, now, "attempts exhausted")
        if not self._retry_allowed():
            self.retries_denied += 1
            return self._finish(r, "failed", status, now, "retry budget exhausted")
        self.retries_used += 1
        exp = self.config.backoff_base_ms * (2 ** (len(r.attempts) - 1))
        ceiling = exp if exp < self.config.backoff_cap_ms else self.config.backoff_cap_ms
        a.backoff_ms = self.rng.next() * ceiling
        self._push(now + a.backoff_ms, "retry", rid)

    # reporting --------------------------------------------------------------
    def summary(self) -> dict:
        done = [self.requests[i] for i in self.completed]
        ok = [r for r in done if r.outcome == "ok"]
        lat = sorted(r.finished - r.arrival for r in ok)

        def pct(p: float) -> float:
            if not lat:
                return 0.0
            return lat[min(len(lat) - 1, int(p * len(lat)))]

        counts: Dict[str, int] = {}
        for r in done:
            counts[r.outcome] = counts.get(r.outcome, 0) + 1
        return {
            "now": self.now,
            "completed": len(done),
            "pending": len(self.pending),
            "outcomes": counts,
            "p50_ms": pct(0.5),
            "p95_ms": pct(0.95),
            "retries_used": self.retries_used,
            "retries_denied": self.retries_denied,
            "backends": [{
                "id": i, "state": self.backends[i].breaker.state,
                "requests": self.backends[i].requests, "errors": self.backends[i].errors,
                "in_flight": self.backends[i].in_flight, "peak_in_flight": self.backends[i].peak_in_flight,
                "transitions": list(self.backends[i].breaker.transitions),
            } for i in self.order],
        }

    def compact(self) -> List[list]:
        """Discrete per-request result used for cross-language parity."""
        out = []
        for rid in sorted(self.requests):
            r = self.requests[rid]
            out.append([r.id, r.outcome, r.status, r.pool,
                        ">".join(f"{a.backend}:{a.outcome}" for a in r.attempts),
                        int(r.finished * 1000) if r.outcome != "pending" else -1])
        return out

    def digest(self) -> str:
        text = "\n".join("|".join(str(x) for x in row) for row in self.compact())
        return hashlib.sha256(text.encode()).hexdigest()


def replay(config: dict, arrivals: List[dict], actions: List[dict]) -> TrafficLab:
    """Run a recorded lab session: actions apply at their virtual time, in order."""
    if len(actions) > LIMITS["actions"]:
        raise ValueError("Too many actions")
    lab = TrafficLab(LabConfig.from_dict(config))
    lab.add_arrivals(arrivals)
    for act in actions:
        t = float(act["t"])
        if t < lab.now:
            raise ValueError("Actions must be in time order")
        lab.run_until(t)
        lab.apply(act)
    lab.run_all()
    return lab
