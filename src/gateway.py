"""
Distributed API Gateway
-----------------------
Features:
  - Load balancing (round-robin + weighted)
  - Circuit breaker (open/half-open/closed)
  - Rate limiting per client (token bucket)
  - Health checks on backends
  - Request routing by path prefix
  - Structured logging
"""
import time, asyncio, logging
import httpx
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum
from collections import deque

logger = logging.getLogger(__name__)

# ── Circuit Breaker ───────────────────────────────────────────────────────────

class CircuitState(str, Enum):
    CLOSED = "closed"        # Normal — requests flow through
    OPEN = "open"            # Failures exceed threshold — requests blocked
    HALF_OPEN = "half_open"  # Testing if backend recovered

@dataclass
class CircuitBreaker:
    """
    3-state circuit breaker per backend.
    
    CLOSED → OPEN:      after `failure_threshold` failures in `window_seconds`
    OPEN → HALF_OPEN:   after `recovery_timeout` seconds
    HALF_OPEN → CLOSED: if next request succeeds
    HALF_OPEN → OPEN:   if next request fails
    """
    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    window_seconds: float = 60.0

    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    probe_in_flight: bool = False
    last_failure_time: float = 0.0
    opened_at: float = 0.0
    recent_failures: deque = field(default_factory=lambda: deque())

    def record_success(self):
        self.success_count += 1
        self.probe_in_flight = False
        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.CLOSED
            self.failure_count = 0
            self.recent_failures.clear()
            logger.info("Circuit CLOSED after successful probe")

    def record_failure(self):
        now = time.time()
        was_probe = self.state == CircuitState.HALF_OPEN
        self.probe_in_flight = False
        self.failure_count += 1
        self.last_failure_time = now
        self.recent_failures.append(now)
        # Remove failures outside window
        cutoff = now - self.window_seconds
        while self.recent_failures and self.recent_failures[0] < cutoff:
            self.recent_failures.popleft()

        if was_probe or len(self.recent_failures) >= self.failure_threshold:
            if self.state != CircuitState.OPEN:
                self.state = CircuitState.OPEN
                self.opened_at = now
                logger.warning(f"Circuit OPENED after {self.failure_threshold} failures")

    def can_pass(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if time.time() - self.opened_at >= self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                logger.info("Circuit HALF_OPEN — sending probe request")
                return True
            return False
        # Reservation is taken only for the backend selected by LoadBalancer.
        return not self.probe_in_flight

# ── Backend ───────────────────────────────────────────────────────────────────

@dataclass
class Backend:
    url: str
    weight: int = 1
    healthy: bool = True
    circuit_breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    total_requests: int = 0
    total_errors: int = 0
    avg_latency_ms: float = 0.0
    _latency_window: deque = field(default_factory=lambda: deque(maxlen=100))

    def record_latency(self, ms: float):
        self._latency_window.append(ms)
        self.avg_latency_ms = sum(self._latency_window) / len(self._latency_window)

    @property
    def error_rate(self) -> float:
        if self.total_requests == 0: return 0.0
        return self.total_errors / self.total_requests

    def is_available(self) -> bool:
        return self.healthy and self.circuit_breaker.can_pass()

    def to_dict(self) -> dict:
        return {
            "url": self.url, "healthy": self.healthy,
            "circuit_state": self.circuit_breaker.state.value,
            "total_requests": self.total_requests,
            "error_rate": round(self.error_rate, 3),
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "weight": self.weight
        }

# ── Load Balancer ─────────────────────────────────────────────────────────────

class LoadBalancer:
    """Round-robin with weight support. Skips unhealthy backends."""
    def __init__(self, backends: List[Backend]):
        self.backends = backends
        self._index = 0
        self._weighted_pool = self._build_pool()

    def _build_pool(self) -> List[Backend]:
        pool = []
        for b in self.backends:
            pool.extend([b] * b.weight)
        return pool

    def next(self) -> Optional[Backend]:
        available = [b for b in self.backends if b.is_available()]
        if not available: return None
        # Weighted round-robin over available backends
        avail_set = set(id(b) for b in available)
        pool = [b for b in self._weighted_pool if id(b) in avail_set]
        if not pool: pool = available
        backend = pool[self._index % len(pool)]
        self._index = (self._index + 1) % len(pool)
        if backend.circuit_breaker.state == CircuitState.HALF_OPEN:
            backend.circuit_breaker.probe_in_flight = True
        return backend

    def refresh_pool(self):
        self._weighted_pool = self._build_pool()

# ── Rate Limiter (in-memory, no Redis dep for gateway core) ──────────────────

class InMemoryRateLimiter:
    """Simple token bucket rate limiter (per client, in-memory)."""
    def __init__(self, capacity: int = 100, refill_rate: float = 10.0):
        self.capacity = capacity
        self.refill_rate = refill_rate
        self._buckets: Dict[str, dict] = {}

    def is_allowed(self, client_id: str) -> bool:
        now = time.time()
        if client_id not in self._buckets:
            self._buckets[client_id] = {"tokens": self.capacity, "last": now}
        b = self._buckets[client_id]
        elapsed = now - b["last"]
        b["tokens"] = min(self.capacity, b["tokens"] + elapsed * self.refill_rate)
        b["last"] = now
        if b["tokens"] >= 1:
            b["tokens"] -= 1
            return True
        return False

# ── Proxy Response ────────────────────────────────────────────────────────────

@dataclass
class ProxyResponse:
    status_code: int
    headers: dict
    body: bytes
    backend_url: str
    latency_ms: float

# ── Route Config ──────────────────────────────────────────────────────────────

@dataclass
class Route:
    prefix: str              # e.g. "/api/v1"
    backends: List[Backend]
    strip_prefix: bool = False
    load_balancer: LoadBalancer = field(init=False)

    def __post_init__(self):
        self.load_balancer = LoadBalancer(self.backends)

# ── Gateway ───────────────────────────────────────────────────────────────────

@dataclass
class GatewayConfig:
    routes: List[Route]
    rate_limit_capacity: int = 100
    rate_limit_refill: float = 10.0
    health_check_interval: float = 30.0

class Gateway:
    """
    Main API Gateway class.
    
    Usage:
        backends = [Backend("http://svc1:8001"), Backend("http://svc2:8002")]
        route = Route(prefix="/api", backends=backends)
        gw = Gateway(GatewayConfig(routes=[route]))
        backend = gw.route_request("/api/users", client_id="user:42")
    """
    def __init__(self, config: GatewayConfig):
        self.config = config
        self.rate_limiter = InMemoryRateLimiter(
            config.rate_limit_capacity, config.rate_limit_refill
        )
        self.total_requests = 0
        self.rejected_requests = 0
        self.start_time = time.time()

    def route_request(self, path: str, client_id: str = "anonymous") -> Optional[Backend]:
        """
        Route an incoming request to an appropriate backend.
        Returns Backend if allowed, None if rate limited or no healthy backends.
        """
        self.total_requests += 1

        # Rate limit check
        if not self.rate_limiter.is_allowed(client_id):
            self.rejected_requests += 1
            logger.warning(f"Rate limited: {client_id}")
            return None

        # Find matching route
        route = self._match_route(path)
        if not route:
            return None

        # Pick backend via load balancer
        backend = route.load_balancer.next()
        if backend:
            backend.total_requests += 1
        return backend

    def record_response(self, backend: Backend, success: bool, latency_ms: float):
        """Call this after a proxied request completes."""
        backend.record_latency(latency_ms)
        if success:
            backend.circuit_breaker.record_success()
        else:
            backend.total_errors += 1
            backend.circuit_breaker.record_failure()

    def _match_route(self, path: str) -> Optional[Route]:
        # Longest prefix match
        best = None
        for route in self.config.routes:
            route_path = path.split("?",1)[0]
            prefix = route.prefix.rstrip("/")
            if not prefix or route_path == prefix or route_path.startswith(prefix + "/"):
                if best is None or len(route.prefix) > len(best.prefix):
                    best = route
        return best

    def get_stats(self) -> dict:
        uptime = time.time() - self.start_time
        routes_info = []
        for route in self.config.routes:
            routes_info.append({
                "prefix": route.prefix,
                "backends": [b.to_dict() for b in route.backends]
            })
        return {
            "uptime_seconds": round(uptime, 1),
            "total_requests": self.total_requests,
            "rejected_requests": self.rejected_requests,
            "rejection_rate": round(self.rejected_requests / max(1, self.total_requests), 3),
            "routes": routes_info
        }

    def mark_backend_health(self, url: str, healthy: bool):
        for route in self.config.routes:
            for backend in route.backends:
                if backend.url == url:
                    backend.healthy = healthy
                    route.load_balancer.refresh_pool()

    async def proxy_request(
        self,
        path: str,
        method: str = "GET",
        headers: Optional[Dict] = None,
        body: Optional[bytes] = None,
        client_id: str = "anonymous",
        _http_client: Optional[httpx.AsyncClient] = None,
    ) -> Tuple[Optional[ProxyResponse], str]:
        """
        Route and forward an HTTP request to a backend.
        Returns (ProxyResponse, "") on success or (None, reason) on failure.
        Reasons: "rate_limited" | "no_route" | "no_healthy_backend" | "timeout" | "connection_error"
        """
        self.total_requests += 1

        if not self.rate_limiter.is_allowed(client_id):
            self.rejected_requests += 1
            logger.warning(f"Rate limited: {client_id}")
            return None, "rate_limited"

        route = self._match_route(path)
        if route is None:
            return None, "no_route"

        backend = route.load_balancer.next()
        if backend is None:
            return None, "no_healthy_backend"

        backend.total_requests += 1
        forward_path = path[len(route.prefix):] if route.strip_prefix else path
        target_url = backend.url.rstrip("/") + (forward_path or "/")

        own_client = _http_client is None
        client = _http_client or httpx.AsyncClient(timeout=30.0)
        start = time.time()
        try:
            hop_by_hop = {"host", "content-length", "transfer-encoding", "connection"}
            fwd_headers = {k: v for k, v in (headers or {}).items()
                           if k.lower() not in hop_by_hop}
            resp = await client.request(method, target_url, headers=fwd_headers, content=body)
            latency_ms = (time.time() - start) * 1000
            self.record_response(backend, success=resp.status_code < 500, latency_ms=latency_ms)
            return ProxyResponse(
                status_code=resp.status_code,
                headers=dict(resp.headers),
                body=resp.content,
                backend_url=backend.url,
                latency_ms=round(latency_ms, 1),
            ), ""
        except httpx.TimeoutException:
            latency_ms = (time.time() - start) * 1000
            self.record_response(backend, success=False, latency_ms=latency_ms)
            return None, "timeout"
        except httpx.RequestError:
            latency_ms = (time.time() - start) * 1000
            self.record_response(backend, success=False, latency_ms=latency_ms)
            return None, "connection_error"
        finally:
            if own_client:
                await client.aclose()
