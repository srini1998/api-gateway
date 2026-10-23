import { useState } from "react";
import type { RequestTrace } from "./engine/lab.ts";
import type { Session } from "./session.ts";

export const clock = (ms: number) => {
  const m = Math.floor(ms / 60000);
  const s = Math.floor((ms % 60000) / 1000);
  const r = Math.floor(ms % 1000);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}.${String(r).padStart(3, "0")}`;
};
export const ms = (v: number) => `${v < 10 ? v.toFixed(1) : Math.round(v)} ms`;

export function remark(r: RequestTrace): { text: string; tone: "ok" | "warn" | "bad" } {
  const total = r.finished - r.arrival;
  const n = r.attempts.length;
  switch (r.outcome) {
    case "ok":
      if (n > 1) return { text: `Rerouted · ${n} attempts · ${ms(total)}`, tone: "warn" };
      return { text: `On time · ${ms(total)}${r.note ? " · canary fallback" : ""}`, tone: "ok" };
    case "failed":
      return { text: `Failed · ${r.note}`, tone: "bad" };
    case "rate_limited":
      return { text: "Turned away · token bucket empty", tone: "bad" };
    case "short_circuited":
      return { text: "Cancelled · every breaker open", tone: "bad" };
    case "no_route":
      return { text: `No such line · ${r.path}`, tone: "bad" };
    default:
      return { text: "In transit", tone: "warn" };
  }
}

export function Board({ s, selected, onSelect }: { s: Session; selected: number | null; onSelect: (id: number) => void }) {
  const [trouble, setTrouble] = useState(false);
  const done = s.lab.completed;
  const rows: RequestTrace[] = [];
  for (let i = done.length - 1; i >= 0 && rows.length < 14; i--) {
    const r = s.lab.requests.get(done[i])!;
    if (trouble && r.outcome === "ok" && r.attempts.length === 1) continue;
    rows.push(r);
  }
  return (
    <div className="board">
      <div className="board-head">
        <h2 id="board-title">
          <span className="pict" aria-hidden="true">→</span> Arrivals
        </h2>
        <div className="seg" role="group" aria-label="Board filter">
          <button type="button" aria-pressed={!trouble} onClick={() => setTrouble(false)}>All</button>
          <button type="button" aria-pressed={trouble} onClick={() => setTrouble(true)}>Disruptions</button>
        </div>
      </div>
      <table aria-labelledby="board-title">
        <thead>
          <tr>
            <th scope="col" className="hide-sm">Time</th>
            <th scope="col">No.</th>
            <th scope="col" className="hide-sm">From</th>
            <th scope="col">Via</th>
            <th scope="col">Code</th>
            <th scope="col">Remarks</th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr>
              <td colSpan={6} className="empty">No requests yet. Dispatch a pattern to start service.</td>
            </tr>
          )}
          {rows.map((r) => {
            const rm = remark(r);
            return (
              <tr key={r.id} className={selected === r.id ? "sel" : ""}>
                <td className="num hide-sm">{clock(r.finished)}</td>
                <td className="num">
                  <button type="button" className="row-btn" onClick={() => onSelect(r.id)} aria-label={`Inspect trace for request ${r.id}`}>
                    {String(r.id).padStart(4, "0")}
                  </button>
                </td>
                <td className="hide-sm">{r.client.replace("client-", "")}</td>
                <td className="via">
                  {r.attempts.length ? r.attempts.map((a) => a.backend).join(" › ") : "—"}
                  {r.pool === "canary" && <span className="tag">canary</span>}
                </td>
                <td className={`num code-${rm.tone}`}>{r.status}</td>
                <td className={`remark ${rm.tone}`}>{rm.text}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function TraceView({ r }: { r: RequestTrace | undefined }) {
  if (!r) {
    return (
      <div className="trace empty-trace">
        <h2>Trace</h2>
        <p>Choose a request number on the board to see every span: admission, each attempt, backoff waits and the final answer.</p>
      </div>
    );
  }
  const end = r.outcome === "pending" ? Math.max(r.arrival + 1, ...r.attempts.map((a) => a.end)) : r.finished;
  const span = Math.max(1, end - r.arrival);
  const pct = (t: number) => `${((t - r.arrival) / span) * 100}%`;
  const rm = remark(r);
  return (
    <div className="trace">
      <h2>
        Trace <span className="num">{String(r.id).padStart(4, "0")}</span>
      </h2>
      <p className="trace-meta">
        {r.client} · {r.path} · {r.pool} pool · <span className={rm.tone}>{rm.text}</span>
      </p>
      <ol className="spans">
        <li>
          <span className="span-label">admit</span>
          <span className="span-track">
            <span className="span-bar admit" style={{ left: 0, width: "2px" }} />
          </span>
          <span className="span-time num">{clock(r.arrival)}</span>
        </li>
        {r.attempts.map((a, i) => (
          <li key={i}>
            <span className="span-label">
              try {i + 1} → {a.backend}
              {a.probe ? " (probe)" : ""}
            </span>
            <span className="span-track">
              <span className={`span-bar ${a.outcome}`} style={{ left: pct(a.start), width: `calc(${pct(a.end)} - ${pct(a.start)} + 2px)` }} />
              {a.backoff_ms > 0 && (
                <span className="span-bar backoff" style={{ left: pct(a.end), width: `calc(${pct(a.end + a.backoff_ms)} - ${pct(a.end)})` }} />
              )}
            </span>
            <span className="span-time num">
              {a.outcome} · {ms(a.end - a.start)}
              {a.backoff_ms > 0 ? ` · wait ${ms(a.backoff_ms)}` : ""}
            </span>
          </li>
        ))}
      </ol>
      {r.note && <p className="trace-note">Gateway note: {r.note}</p>}
    </div>
  );
}
