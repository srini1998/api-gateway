"""Vercel Python function: replay a recorded traffic-lab run through the Python reference.

Accepts only bounded JSON (config, arrivals, actions); executes no visitor code and makes
no outbound requests. Returns the digest of the per-request results for comparison with
the browser's TypeScript port.
"""
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from traffic_lab import replay, LIMITS  # noqa: E402

MAX_BODY = 400_000
WINDOW_S, PER_WINDOW = 60, 20
_hits: dict = {}


def _limited(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _hits.get(ip, []) if now - t < WINDOW_S]
    if len(_hits) > 5000:
        _hits.clear()
    recent.append(now)
    _hits[ip] = recent
    return len(recent) > PER_WINDOW


class handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(200, {"engine": "traffic_lab.py", "limits": LIMITS, "method": "POST {config, arrivals, actions}"})

    def do_POST(self):
        ip = (self.headers.get("x-forwarded-for") or "unknown").split(",")[0].strip()
        if _limited(ip):
            return self._send(429, {"error": "Too many checks from this address; wait a minute."})
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            return self._send(413, {"error": "Body must be JSON under 400 KB."})
        try:
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Expected an object")
            config, arrivals, actions = data.get("config"), data.get("arrivals"), data.get("actions", [])
            if not isinstance(config, dict) or not isinstance(arrivals, list) or not isinstance(actions, list):
                raise ValueError("Expected config object, arrivals list and actions list")
            if len(arrivals) > LIMITS["arrivals"] or len(actions) > LIMITS["actions"]:
                raise ValueError("Run exceeds lab limits")
            started = time.perf_counter()
            lab = replay(config, arrivals, actions)
            elapsed = (time.perf_counter() - started) * 1000
        except (ValueError, KeyError, TypeError) as exc:
            return self._send(400, {"error": str(exc)[:200]})
        summary = lab.summary()
        self._send(200, {
            "digest": lab.digest(),
            "requests": len(lab.requests),
            "outcomes": summary["outcomes"],
            "retries_used": summary["retries_used"],
            "engine_ms": round(elapsed, 2),
        })
