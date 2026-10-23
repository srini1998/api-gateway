import { useEffect, useRef, useState } from "react";
import { RouteMap } from "./RouteMap.tsx";
import { Board, TraceView, clock, ms } from "./Board.tsx";
import { buildConfig, DEFAULT_POLICY, makePattern, PATTERNS, Session, CLOCK_LIMIT, type Policy } from "./session.ts";
import type { BackendSpec } from "./engine/lab.ts";

const SPEEDS = [
  { v: 0.05, label: "1/20" },
  { v: 0.25, label: "1/4" },
  { v: 1, label: "Real time" },
];
const LATENCY = [
  { v: 40, label: "Fast" },
  { v: 160, label: "Slow" },
  { v: 600, label: "Stalled" },
];
const ERRORS = [0, 0.2, 0.6, 1];

function useReducedMotion() {
  const [r, setR] = useState(() => typeof matchMedia !== "undefined" && matchMedia("(prefers-reduced-motion: reduce)").matches);
  useEffect(() => {
    const m = matchMedia("(prefers-reduced-motion: reduce)");
    const f = () => setR(m.matches);
    m.addEventListener("change", f);
    return () => m.removeEventListener("change", f);
  }, []);
  return r;
}

type Verify =
  | { state: "idle" }
  | { state: "busy" }
  | { state: "done"; match: boolean; local: string; remote: string; requests: number; actions: number }
  | { state: "error"; message: string };

export default function App() {
  const reduced = useReducedMotion();
  const [policy, setPolicy] = useState<Policy>(DEFAULT_POLICY);
  const [draft, setDraft] = useState<Policy>(DEFAULT_POLICY);
  const sessRef = useRef<Session>(new Session(buildConfig(DEFAULT_POLICY)));
  const [, setFrame] = useState(0);
  const [speed, setSpeed] = useState(0.25);
  const [paused, setPaused] = useState(false);
  const [selected, setSelected] = useState<number | null>(null);
  const [message, setMessage] = useState("Ready. Choose a traffic pattern to dispatch.");
  const [verify, setVerify] = useState<Verify>({ state: "idle" });
  const speedRef = useRef(speed);
  const pausedRef = useRef(paused);
  speedRef.current = speed;
  pausedRef.current = paused;

  useEffect(() => {
    let raf = 0;
    let last = performance.now();
    let acc = 0;
    const loop = (now: number) => {
      const dt = Math.min(100, now - last);
      last = now;
      const s = sessRef.current;
      if (!pausedRef.current && s.vt < CLOCK_LIMIT) s.advance(s.vt + dt * speedRef.current);
      acc += dt;
      if (acc > 33) {
        acc = 0;
        setFrame((f) => (f + 1) % 1e9);
      }
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, []);

  const s = sessRef.current;
  const sum = s.lab.summary();
  const admitted = (sum.outcomes.ok ?? 0) + (sum.outcomes.failed ?? 0) + (sum.outcomes.short_circuited ?? 0);
  const budget = 3 + s.config.retry_budget_ratio * s.lab.originals;

  const dispatch = (id: (typeof PATTERNS)[number]["id"]) => {
    const n = s.dispatch(makePattern(s, id));
    setVerify({ state: "idle" });
    setMessage(n ? `Dispatched ${n} requests (${PATTERNS.find((p) => p.id === id)!.label}).` : "This run is full (2,000 requests or 10 virtual minutes). Restart the lab to keep going.");
  };
  const patch = (b: BackendSpec, p: Partial<BackendSpec>, what: string) => {
    if (!s.act({ op: "backend", id: b.id, patch: p })) return setMessage("Action limit reached for this run (60). Restart the lab.");
    setVerify({ state: "idle" });
    setMessage(`Platform ${b.id}: ${what} at ${clock(s.vt)}.`);
  };
  const restart = (p: Policy) => {
    sessRef.current = new Session(buildConfig(p));
    setPolicy(p);
    setSelected(null);
    setVerify({ state: "idle" });
    setMessage("Fresh run started with the rules shown.");
  };
  const setCanary = (pct: number) => {
    if (!s.act({ op: "canary", percent: pct })) return setMessage("Action limit reached for this run (60). Restart the lab.");
    setPolicy({ ...policy, canary_percent: pct });
    setMessage(`Canary split set to ${pct}% at ${clock(s.vt)}.`);
  };

  const runVerify = async () => {
    if (!s.arrivals.length) return setMessage("Dispatch some traffic first, then check it.");
    setVerify({ state: "busy" });
    try {
      const local = await s.replayAll().digest();
      const res = await fetch("/api/verify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config: s.config, arrivals: s.arrivals, actions: s.actions }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      setVerify({ state: "done", match: data.digest === local, local, remote: data.digest, requests: data.requests, actions: s.actions.length });
    } catch (e) {
      setVerify({ state: "error", message: e instanceof Error ? e.message : "Request failed" });
    }
  };

  const sel = selected != null ? s.lab.requests.get(selected) : undefined;
  const backends = s.lab.order.map((id) => s.lab.backends.get(id)!);
  const trackEnd = Math.max(1000, s.vt);

  const stats = [
      { k: "Delivered", v: admitted ? `${Math.round(((sum.outcomes.ok ?? 0) / admitted) * 1000) / 10}%` : "—", hint: "OK ÷ requests admitted past the rate limiter" },
      { k: "p50", v: sum.p50_ms ? ms(sum.p50_ms) : "—", hint: "Median end-to-end, retries included" },
      { k: "p95", v: sum.p95_ms ? ms(sum.p95_ms) : "—", hint: "95th percentile end-to-end" },
      { k: "Retries", v: `${sum.retries_used} / ${Math.floor(budget)}`, hint: "Used / current retry budget" },
      { k: "Budget denied", v: String(sum.retries_denied), hint: "Retries refused to stop a retry storm" },
      { k: "429 · 404 · 503", v: `${sum.outcomes.rate_limited ?? 0} · ${sum.outcomes.no_route ?? 0} · ${sum.outcomes.short_circuited ?? 0}`, hint: "Turned away without touching a backend" },
  ];

  return (
    <>
      <a className="skip" href="#lab">Skip to the lab</a>
      <header className="sign">
        <div className="sign-mark" aria-hidden="true">G</div>
        <div className="sign-name">
          <span className="sign-kicker">Gateway Central</span>
          <span className="sign-title">Traffic Lab</span>
        </div>
        <div className="sign-clock" aria-live="off">
          <span className="sign-kicker">Virtual clock</span>
          <span className="num big">{clock(s.vt)}</span>
        </div>
      </header>

      <section className="intro" aria-labelledby="intro-title">
        <h1 id="intro-title">Send traffic through a gateway. Break a platform. Watch it cope.</h1>
        <p>
          Every request below is routed by the gateway's actual policy code: weighted round-robin, a canary split, per-backend
          circuit breakers, retries with jittered backoff and a retry budget, and per-client rate limits. The backends are
          simulated so you can kill them safely; the routing decisions are not.
        </p>
      </section>

      <main id="lab" className="lab">
        <section className="map-panel" aria-labelledby="map-h">
          <h2 id="map-h" className="panel-title">
            <span className="line-no">1</span> Route map
          </h2>
          <RouteMap s={s} reduced={reduced} />
          <ul className="legend" aria-label="Map legend">
            <li><i className="dot out" /> request on its way</li>
            <li><i className="dot retry" /> retry</li>
            <li><i className="dot probe" /> breaker test train</li>
            <li><i className="dot waiting" /> waiting out backoff</li>
            <li><i className="bar thick" /> line width = weight</li>
            <li><i className="bar dashed" /> canary line</li>
          </ul>
          <dl className="stats">
            {stats.map((x) => (
              <div key={x.k} title={x.hint}>
                <dt>{x.k}</dt>
                <dd className="num">{x.v}</dd>
              </div>
            ))}
          </dl>
        </section>

        <aside className="dispatch" aria-labelledby="dispatch-h">
          <h2 id="dispatch-h" className="panel-title">
            <span className="line-no">2</span> Dispatch
          </h2>
          <div className="patterns">
            {PATTERNS.map((p) => (
              <button key={p.id} type="button" className="pattern" onClick={() => dispatch(p.id)}>
                <span className="p-label">{p.label}</span>
                <span className="p-detail">{p.detail}</span>
              </button>
            ))}
          </div>
          <div className="row">
            <span className="field-label" id="speed-l">Playback</span>
            <div className="seg" role="group" aria-labelledby="speed-l">
              {SPEEDS.map((x) => (
                <button key={x.v} type="button" aria-pressed={speed === x.v} onClick={() => setSpeed(x.v)}>{x.label}</button>
              ))}
            </div>
          </div>
          <div className="row">
            <button type="button" className="ghost" onClick={() => setPaused(!paused)} aria-pressed={paused}>{paused ? "Resume clock" : "Pause clock"}</button>
            <button type="button" className="ghost" onClick={() => restart(policy)}>Restart lab</button>
          </div>
          <p className="status" role="status">{message}</p>
          <p className="quota num">
            {s.arrivalsLeft} requests · {s.actionsLeft} actions left in this run
          </p>

          <div className="verify">
            <h3>Cross-check</h3>
            <p>Replay this whole run, including every platform change you made, through the Python reference on the server and compare results.</p>
            <button type="button" className="primary" onClick={runVerify} disabled={verify.state === "busy"}>
              {verify.state === "busy" ? "Replaying…" : "Check with Python reference"}
            </button>
            <div role="status" className="verify-out">
              {verify.state === "done" && (
                <p className={verify.match ? "ok" : "bad"}>
                  {verify.match ? "Identical." : "Mismatch."} {verify.requests} requests, {verify.actions} actions. Browser{" "}
                  <code>{verify.local.slice(0, 12)}</code> · Python <code>{verify.remote.slice(0, 12)}</code>
                </p>
              )}
              {verify.state === "error" && <p className="bad">Python check unavailable: {verify.message}</p>}
            </div>
          </div>
        </aside>

        <section className="platforms" aria-labelledby="plat-h">
          <h2 id="plat-h" className="panel-title">
            <span className="line-no">3</span> Platforms
          </h2>
          <div className="plat-grid">
            {backends.map((b) => {
              const st = b.breaker.state;
              return (
                <article key={b.spec.id} className={`plat state-${st} ${b.spec.down ? "is-down" : ""}`} aria-label={`Platform ${b.spec.id}`}>
                  <header>
                    <span className="roundel">{b.spec.id}</span>
                    <div>
                      <h3>{b.spec.pool === "canary" ? "Canary" : "Stable"} · Platform {b.spec.id}</h3>
                      <p className="plat-state">
                        {b.spec.down ? "Down · " : ""}
                        {st === "closed" ? "Breaker closed" : st === "open" ? "Breaker open" : "Breaker half-open"}
                      </p>
                    </div>
                  </header>
                  <p className="plat-nums num">
                    {b.requests} sent · {b.errors} failed · {b.in_flight} in flight
                  </p>
                  <div className="ctl">
                    <button type="button" className={b.spec.down ? "primary" : "danger"} onClick={() => patch(b.spec, { down: !b.spec.down }, b.spec.down ? "restored" : "killed")}>
                      {b.spec.down ? `Restore ${b.spec.id}` : `Kill ${b.spec.id}`}
                    </button>
                  </div>
                  <div className="ctl">
                    <span className="field-label" id={`lat-${b.spec.id}`}>Latency</span>
                    <div className="seg" role="group" aria-labelledby={`lat-${b.spec.id}`}>
                      {LATENCY.map((l) => (
                        <button key={l.v} type="button" aria-pressed={Math.abs(b.spec.latency_ms - l.v) < 15} onClick={() => patch(b.spec, { latency_ms: l.v }, `latency ${l.v} ms`)}>
                          {l.label}
                        </button>
                      ))}
                    </div>
                  </div>
                  <div className="ctl">
                    <span className="field-label" id={`err-${b.spec.id}`}>5xx rate</span>
                    <div className="seg" role="group" aria-labelledby={`err-${b.spec.id}`}>
                      {ERRORS.map((e) => (
                        <button key={e} type="button" aria-pressed={Math.abs(b.spec.error_rate - e) < 0.04} onClick={() => patch(b.spec, { error_rate: e }, `errors ${e * 100}%`)}>
                          {e * 100}%
                        </button>
                      ))}
                    </div>
                  </div>
                  <div className="ctl">
                    <span className="field-label">Weight</span>
                    <div className="stepper">
                      <button type="button" aria-label={`Lower weight of ${b.spec.id}`} disabled={b.spec.weight <= 0} onClick={() => patch(b.spec, { weight: b.spec.weight - 1 }, `weight ${b.spec.weight - 1}`)}>−</button>
                      <span className="num" aria-live="polite">{b.spec.weight}</span>
                      <button type="button" aria-label={`Raise weight of ${b.spec.id}`} disabled={b.spec.weight >= 5} onClick={() => patch(b.spec, { weight: b.spec.weight + 1 }, `weight ${b.spec.weight + 1}`)}>+</button>
                    </div>
                  </div>
                  <div className="timeline" aria-label={`Breaker history for ${b.spec.id}`}>
                    {(() => {
                      const segs: { from: number; to: number; st: string }[] = [];
                      let cur = "closed";
                      let at = 0;
                      for (const tr of b.breaker.transitions) {
                        segs.push({ from: at, to: tr.t, st: cur });
                        cur = tr.to;
                        at = tr.t;
                      }
                      segs.push({ from: at, to: trackEnd, st: cur });
                      return segs.map((g, i) => (
                        <span key={i} className={`seg-${g.st}`} style={{ left: `${(g.from / trackEnd) * 100}%`, width: `${((g.to - g.from) / trackEnd) * 100}%` }} />
                      ));
                    })()}
                  </div>
                  <p className="tl-note">{b.breaker.transitions.length} signal changes</p>
                </article>
              );
            })}
          </div>
          <div className="canary-row">
            <span className="field-label" id="canary-l">Canary split (live, sticky per client)</span>
            <div className="seg" role="group" aria-labelledby="canary-l">
              {[0, 15, 50, 100].map((p) => (
                <button key={p} type="button" aria-pressed={s.lab.config.canary_percent === p} onClick={() => setCanary(p)}>{p}%</button>
              ))}
            </div>
          </div>
        </section>

        <section className="board-wrap" aria-label="Arrivals board and trace">
          <Board s={s} selected={selected} onSelect={setSelected} />
          <TraceView r={sel} />
        </section>

        <section className="rules" aria-labelledby="rules-h">
          <h2 id="rules-h" className="panel-title">
            <span className="line-no">4</span> House rules
          </h2>
          <p className="rules-intro">These apply to a fresh run. Current run: {policy.max_attempts} attempts, {policy.timeout_ms} ms timeout, breaker trips after {policy.breaker_threshold} failures and rests {policy.breaker_recovery_ms / 1000} s.</p>
          <form
            className="rules-form"
            onSubmit={(e) => {
              e.preventDefault();
              restart(draft);
            }}
          >
            {(
              [
                ["max_attempts", "Attempts per request", [1, 2, 3, 4, 5]],
                ["timeout_ms", "Per-try timeout (ms)", [100, 250, 500, 1000]],
                ["breaker_threshold", "Failures to trip breaker", [2, 3, 5, 10, 20]],
                ["breaker_recovery_ms", "Breaker rest before probe (ms)", [500, 1000, 3000, 8000]],
                ["retry_budget_ratio", "Retry budget (share of traffic)", [0, 0.1, 0.2, 0.5, 1]],
              ] as const
            ).map(([key, label, opts]) => (
              <label key={key}>
                <span>{label}</span>
                <select value={draft[key]} onChange={(e) => setDraft({ ...draft, [key]: Number(e.target.value) })}>
                  {opts.map((o) => (
                    <option key={o} value={o}>{key === "retry_budget_ratio" ? `${o * 100}% + 3` : o}</option>
                  ))}
                </select>
              </label>
            ))}
            <button type="submit" className="primary">Restart with these rules</button>
          </form>
        </section>

        <section className="notes" aria-labelledby="notes-h">
          <h2 id="notes-h" className="panel-title">
            <span className="line-no">5</span> What is real here
          </h2>
          <div className="notes-grid">
            <div>
              <h3>Real</h3>
              <p>
                The routing, canary, breaker, retry, budget and rate-limit decisions. They come from <code>traffic_lab.py</code> in the
                gateway repository; this page runs a line-for-line TypeScript port. Four Python golden traces must be reproduced
                exactly by the port before a build ships, and the cross-check button replays your own run on the server in Python.
              </p>
            </div>
            <div>
              <h3>Simulated</h3>
              <p>
                Backends, latency and failures. Time is a virtual millisecond clock in an event queue, so requests genuinely overlap and
                a half-open breaker admits one probe while others wait, but no sockets are opened and nothing here measures network
                throughput.
              </p>
            </div>
            <div>
              <h3>Limits</h3>
              <p>
                2,000 requests, 60 platform changes and 10 virtual minutes per run; up to 6 backends. The cross-check endpoint takes
                only these bounded inputs and runs no code you send. Single-process state: this is not a multi-node gateway benchmark.
              </p>
            </div>
          </div>
          <p className="src">
            Source: <a href="https://github.com/srini1998/api-gateway">github.com/srini1998/api-gateway</a>
          </p>
        </section>
      </main>
    </>
  );
}
