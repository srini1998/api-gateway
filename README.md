# Distributed API Gateway

A failing backend should stop normal traffic while a single recovery probe checks its health.

Trip the circuit, allow one half-open request, and reject a second until completion. /api must not match /apiary.

See [review](docs/review.md) and [actual checks](docs/test-results.txt) for data, validation and limitations.

```sh
python -m pytest tests -q
```

No production-readiness or independent model-quality guarantee is claimed.

## Traffic Lab (next-level gateway policies) — live website

`src/traffic_lab.py` is a deterministic, event-driven model of the gateway's routing policies:
smooth weighted round-robin (nginx algorithm), a sticky canary split (FNV-1a hash of the client),
per-backend circuit breakers with a single half-open probe, retries with capped exponential backoff
and full jitter that move to a different backend, a retry budget (3 + 20% of admitted traffic by
default) to stop retry storms, per-client token buckets, per-try timeouts and a span trace per request.
Requests overlap on a virtual millisecond clock, so a half-open breaker really blocks concurrent
traffic while its probe is in flight.

`site/` is the public website: an interactive traffic lab where you dispatch traffic patterns, kill or
slow a backend mid-run, change weights and the canary split, and read the arrivals board and per-request
traces. The browser runs `site/src/engine/lab.ts`, a line-for-line TypeScript port. Parity is enforced:
`tests/test_traffic_lab.py` writes golden traces to `tests/golden/`, and `site/tests/parity.test.ts`
(run in the Vercel build before shipping) must reproduce them exactly. `api/verify.py` is a Vercel
Python function that replays a visitor's recorded run through the Python reference and returns its digest.

What is simulated: the backends (latency, jitter, error rate, down). No sockets are opened in the lab; it
is not a throughput benchmark. Limits per run: 2,000 requests, 60 actions, 6 backends, 10 virtual minutes.

```sh
python -m pytest tests -q              # Python reference + verify endpoint
cd site && npm ci && npm test          # TypeScript parity against the golden traces
npm run build
```
