import type { Session } from "./session.ts";
import type { RequestTrace } from "./engine/lab.ts";

type Pt = [number, number];
const W = 960;
const H = 440;
const HUB: Pt = [330, 200];
const ORIGIN: Pt = [70, 200];
const SIDING: Pt = [330, 400];
const STATION_X = 880;

function lineFor(i: number, n: number): Pt[] {
  const span = Math.min(300, (n - 1) * 100);
  const y = n === 1 ? HUB[1] : HUB[1] - span / 2 + (span * i) / (n - 1);
  const dy = Math.abs(y - HUB[1]);
  return [HUB, [470, HUB[1]], [470 + dy, y], [STATION_X, y]];
}

function along(pts: Pt[], f: number): Pt {
  const segs = pts.slice(1).map((p, i) => Math.hypot(p[0] - pts[i][0], p[1] - pts[i][1]));
  const total = segs.reduce((a, b) => a + b, 0);
  let d = Math.max(0, Math.min(1, f)) * total;
  for (let i = 0; i < segs.length; i++) {
    if (d <= segs[i] || i === segs.length - 1) {
      const k = segs[i] ? d / segs[i] : 0;
      return [pts[i][0] + (pts[i + 1][0] - pts[i][0]) * k, pts[i][1] + (pts[i + 1][1] - pts[i][1]) * k];
    }
    d -= segs[i];
  }
  return pts[pts.length - 1];
}

const path = (pts: Pt[]) => "M" + pts.map((p) => p.join(",")).join(" L");

const STATE_LABEL = { closed: "In service", open: "Suspended", half_open: "Test train" } as const;

export function RouteMap({ s, reduced }: { s: Session; reduced: boolean }) {
  const lab = s.lab;
  const vt = s.vt;
  const ids = lab.order;
  const lines = ids.map((_, i) => lineFor(i, ids.length));
  const trains: { x: number; y: number; kind: string; key: string }[] = [];
  const flashes: { x: number; y: number; ok: boolean; key: string }[] = [];
  const waiting: RequestTrace[] = [];
  let inbound = 0;
  let turned = 0;
  for (const r of lab.requests.values()) {
    if (r.arrival > vt) continue;
    if (r.outcome === "rate_limited" || r.outcome === "no_route" || r.outcome === "short_circuited") turned++;
    if (vt - r.arrival < 140 && !reduced) {
      const [x, y] = along([ORIGIN, HUB], (vt - r.arrival) / 140);
      if (r.outcome === "rate_limited" || r.outcome === "no_route" || r.outcome === "short_circuited") {
        const [sx, sy] = along([HUB, SIDING], (vt - r.arrival) / 140);
        trains.push({ x: sx, y: sy, kind: "turned", key: `t${r.id}` });
      } else trains.push({ x, y, kind: "in", key: `i${r.id}` });
      inbound++;
    }
    r.attempts.forEach((a, n) => {
      const li = ids.indexOf(a.backend);
      if (a.start <= vt && vt < a.end) {
        const f = reduced ? 1 : (vt - a.start) / (a.end - a.start);
        const [x, y] = along(lines[li], f);
        trains.push({ x, y, kind: a.probe ? "probe" : n > 0 ? "retry" : "out", key: `a${r.id}-${n}` });
      } else if (a.end <= vt && vt - a.end < 220) {
        const [x, y] = lines[li][3];
        flashes.push({ x, y, ok: a.outcome === "ok", key: `f${r.id}-${n}` });
      }
    });
    const last = r.attempts[r.attempts.length - 1];
    if (r.outcome === "pending" && last && last.end <= vt) waiting.push(r);
  }

  return (
    <svg className="map" viewBox={`0 0 ${W} ${H}`} role="img" aria-labelledby="map-title map-desc">
      <title id="map-title">Live route map</title>
      <desc id="map-desc">
        Requests travel from clients to the gateway interchange and out along one line per backend platform. {inbound} entering,{" "}
        {trains.length - inbound} on lines, {waiting.length} waiting to retry, {turned} turned away so far.
      </desc>
      <defs>
        <pattern id="hatch" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <rect width="8" height="8" fill="var(--panel)" />
          <line x1="0" y1="0" x2="0" y2="8" stroke="var(--red)" strokeWidth="3" />
        </pattern>
      </defs>
      <g className="grid-lines" aria-hidden="true">
        {Array.from({ length: 13 }, (_, i) => (
          <line key={i} x1={i * 80} y1="0" x2={i * 80} y2={H} />
        ))}
      </g>
      <path d={path([ORIGIN, HUB])} className="track trunk" />
      <path d={path([HUB, SIDING])} className="track siding" />
      {ids.map((id, i) => {
        const b = lab.backends.get(id)!;
        const st = b.breaker.state;
        const w = b.spec.weight;
        return (
          <g key={id} className={`line state-${st} ${b.spec.down ? "is-down" : ""} pool-${b.spec.pool}`}>
            <path d={path(lines[i])} className="track" style={{ strokeWidth: w === 0 ? 2 : 4 + w * 3 }} />
            {w === 0 && <path d={path(lines[i])} className="track drained" />}
          </g>
        );
      })}
      {/* stations */}
      <g className="station origin">
        <circle cx={ORIGIN[0]} cy={ORIGIN[1]} r="22" />
        <text x={ORIGIN[0]} y={ORIGIN[1] + 5} textAnchor="middle">IN</text>
        <text className="caption" x={ORIGIN[0]} y={ORIGIN[1] + 50} textAnchor="middle">Clients</text>
      </g>
      <g className="station hub">
        <rect x={HUB[0] - 30} y={HUB[1] - 30} width="60" height="60" rx="6" />
        <text x={HUB[0]} y={HUB[1] + 8} textAnchor="middle">G</text>
        <text className="caption" x={HUB[0]} y={HUB[1] - 44} textAnchor="middle">Gateway</text>
      </g>
      <g className="station siding-stop">
        <rect x={SIDING[0] - 70} y={SIDING[1] - 4} width="140" height="8" />
        <text className="caption" x={SIDING[0] + 84} y={SIDING[1] + 5}>Turned away · {turned}</text>
      </g>
      {ids.map((id, i) => {
        const b = lab.backends.get(id)!;
        const [x, y] = lines[i][3];
        const st = b.breaker.state;
        return (
          <g key={id} className={`station platform state-${st} ${b.spec.down ? "is-down" : ""}`}>
            <circle cx={x} cy={y} r="20" fill={b.spec.down ? "url(#hatch)" : undefined} />
            <text x={x} y={y + 6} textAnchor="middle">{id}</text>
            <text className="caption" x={x} y={y + 38} textAnchor="middle">
              {b.spec.pool === "canary" ? "Canary · " : ""}
              {b.spec.down ? "Down" : STATE_LABEL[st]}
            </text>
            <g className="signal" aria-hidden="true">
              <rect x={x + 28} y={y - 18} width="12" height="36" rx="6" />
              <circle cx={x + 34} cy={y - 9} r="4" className={st === "closed" ? "lamp on-ok" : "lamp"} />
              <circle cx={x + 34} cy={y} r="4" className={st === "half_open" ? "lamp on-warn" : "lamp"} />
              <circle cx={x + 34} cy={y + 9} r="4" className={st === "open" ? "lamp on-stop" : "lamp"} />
            </g>
          </g>
        );
      })}
      <g className="trains" aria-hidden="true">
        {flashes.map((f) => (
          <circle key={f.key} cx={f.x} cy={f.y} r="26" className={f.ok ? "flash ok" : "flash bad"} />
        ))}
        {trains.map((t) => (
          <circle key={t.key} cx={t.x} cy={t.y} r={t.kind === "probe" ? 8 : 5} className={`train ${t.kind}`} />
        ))}
        {waiting.slice(0, 24).map((r, i) => (
          <circle key={`w${r.id}`} cx={HUB[0] + 44 + (i % 6) * 9} cy={HUB[1] + 44 + Math.floor(i / 6) * 9} r="3.5" className="train waiting" />
        ))}
      </g>
    </svg>
  );
}
