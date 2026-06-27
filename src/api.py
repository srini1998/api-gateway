import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, JSONResponse
from pydantic import BaseModel
from gateway import Gateway, GatewayConfig, Route, Backend

app = FastAPI(title="Distributed API Gateway", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Internal demo gateway (simulated backends) ────────────────────────────────
_demo_backends = [Backend(f"http://backend-{i}.internal:8000", weight=1) for i in range(1, 4)]
gw = Gateway(GatewayConfig(
    routes=[Route("/api", _demo_backends), Route("/health", [_demo_backends[0]])],
    rate_limit_capacity=100, rate_limit_refill=10.0,
))

# ── Live proxy gateway (httpbin.org as real backends for demo) ────────────────
_proxy_backends = [
    Backend("https://httpbin.org", weight=2),
    Backend("https://httpbin.org", weight=1),
]
proxy_gw = Gateway(GatewayConfig(
    routes=[Route("/proxy", _proxy_backends, strip_prefix=True)],
    rate_limit_capacity=50, rate_limit_refill=5.0,
))


class RouteRequest(BaseModel):
    path: str
    client_id: str = "anonymous"


@app.get("/", response_class=HTMLResponse)
async def root():
    return """<html><head><title>API Gateway</title>
    <style>body{font-family:-apple-system,sans-serif;max-width:800px;margin:60px auto;padding:20px;background:#0d1117;color:#e6edf3}
    h1{color:#58a6ff}h2{color:#79c0ff;border-bottom:1px solid #30363d;padding-bottom:8px}
    pre{background:#161b22;padding:16px;border-radius:8px;border:1px solid #30363d}a{color:#58a6ff}
    .badge{background:#238636;color:white;padding:2px 8px;border-radius:12px;font-size:12px}</style></head>
    <body><h1>🔀 Distributed API Gateway</h1>
    <p><span class="badge">v2.0.0</span>&nbsp; Round-robin load balancer with circuit breaker, rate limiting, and <strong>live HTTP proxying</strong>.</p>
    <h2>Live Proxy (httpbin.org backends)</h2>
    <pre>GET  /proxy/get          → httpbin.org/get  (real HTTP forward)
GET  /proxy/uuid         → httpbin.org/uuid
POST /proxy/post         → httpbin.org/post
GET  /proxy/status/418   → httpbin.org/status/418</pre>
    <h2>Demo & Stats</h2>
    <pre>POST /route        — Simulate routing (returns backend URL)
GET  /stats        — Gateway statistics
GET  /backends     — Backend health status
GET  /simulate     — Run 20 simulated requests
GET  /docs         — Swagger UI</pre>
    <p><a href="/docs">📖 Docs</a> | <a href="/stats">📊 Stats</a> | <a href="/proxy/get">▶ Live Proxy</a> | <a href="/simulate">🔁 Simulate</a></p>
    </body></html>"""


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/route")
async def route(req: RouteRequest):
    backend = gw.route_request(req.path, req.client_id)
    if backend is None:
        return {"routed": False, "reason": "rate_limited or no healthy backends"}
    gw.record_response(backend, success=True, latency_ms=42.0)
    return {"routed": True, "backend": backend.url, "circuit": backend.circuit_breaker.state.value}


@app.get("/stats")
async def stats():
    return {
        "demo_gateway": gw.get_stats(),
        "proxy_gateway": proxy_gw.get_stats(),
    }


@app.get("/backends")
async def backend_status():
    return {
        "demo": [b.to_dict() for b in _demo_backends],
        "proxy": [b.to_dict() for b in _proxy_backends],
    }


@app.get("/simulate")
async def simulate():
    results = []
    for i in range(20):
        b = gw.route_request(f"/api/test/{i}", f"user_{i % 5}")
        if b:
            gw.record_response(b, success=(i % 7 != 0), latency_ms=40 + i % 30)
            results.append({"request": i + 1, "backend": b.url, "circuit": b.circuit_breaker.state.value})
    return {"requests_simulated": 20, "results": results, "stats": gw.get_stats()}


@app.api_route("/proxy/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy(request: Request, path: str):
    """Forward request to a real backend via the proxy gateway."""
    client_id = request.client.host if request.client else "anonymous"
    body = await request.body()

    result, error = await proxy_gw.proxy_request(
        path=f"/proxy/{path}",
        method=request.method,
        headers=dict(request.headers),
        body=body or None,
        client_id=client_id,
    )

    if result is None:
        status_map = {
            "rate_limited": 429,
            "no_route": 404,
            "no_healthy_backend": 503,
            "timeout": 504,
            "connection_error": 502,
        }
        return JSONResponse({"error": error}, status_code=status_map.get(error, 502))

    skip = {"content-encoding", "transfer-encoding", "connection", "keep-alive"}
    safe_headers = {k: v for k, v in result.headers.items() if k.lower() not in skip}

    return Response(
        content=result.body,
        status_code=result.status_code,
        headers=safe_headers,
    )
