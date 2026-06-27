import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__),'..','src'))
import pytest
import httpx
from gateway import (Gateway, GatewayConfig, Route, Backend,
                     CircuitBreaker, CircuitState, LoadBalancer, InMemoryRateLimiter,
                     ProxyResponse)

def make_backends(n=2):
    return [Backend(url=f"http://svc{i}:800{i}") for i in range(1, n+1)]

def make_gateway(prefixes=None, backends=None):
    if backends is None: backends = make_backends(2)
    routes = [Route(prefix=p, backends=backends) for p in (prefixes or ["/api"])]
    return Gateway(GatewayConfig(routes=routes))

class TestCircuitBreaker:
    def test_starts_closed(self):
        cb = CircuitBreaker()
        assert cb.state == CircuitState.CLOSED

    def test_opens_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=3, window_seconds=60)
        for _ in range(3): cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_open_blocks_requests(self):
        cb = CircuitBreaker(failure_threshold=1)
        cb.record_failure()
        assert not cb.can_pass()

    def test_success_closes_half_open(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0)
        cb.record_failure()
        cb.can_pass()  # triggers HALF_OPEN
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_failure_in_half_open_reopens(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0)
        cb.record_failure()
        cb.can_pass()  # HALF_OPEN
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

class TestLoadBalancer:
    def test_returns_backend(self):
        lb = LoadBalancer(make_backends(3))
        assert lb.next() is not None

    def test_round_robin_distributes(self):
        backends = make_backends(3)
        lb = LoadBalancer(backends)
        seen = set()
        for _ in range(6): seen.add(lb.next().url)
        assert len(seen) == 3

    def test_skips_unhealthy(self):
        backends = make_backends(2)
        backends[0].healthy = False
        lb = LoadBalancer(backends)
        for _ in range(5):
            b = lb.next()
            assert b.url == backends[1].url

    def test_returns_none_all_unhealthy(self):
        backends = make_backends(2)
        for b in backends: b.healthy = False
        lb = LoadBalancer(backends)
        assert lb.next() is None

class TestRateLimiter:
    def test_allows_within_capacity(self):
        rl = InMemoryRateLimiter(capacity=5, refill_rate=0.1)
        for _ in range(5): assert rl.is_allowed("client1")

    def test_blocks_over_capacity(self):
        rl = InMemoryRateLimiter(capacity=3, refill_rate=0.01)
        for _ in range(3): rl.is_allowed("client2")
        assert not rl.is_allowed("client2")

    def test_different_clients_independent(self):
        rl = InMemoryRateLimiter(capacity=2, refill_rate=0.01)
        rl.is_allowed("c1"); rl.is_allowed("c1"); rl.is_allowed("c1")
        assert rl.is_allowed("c2")

class TestGateway:
    def test_routes_request(self):
        gw = make_gateway()
        b = gw.route_request("/api/users", "client1")
        assert b is not None

    def test_returns_none_no_matching_route(self):
        gw = make_gateway(prefixes=["/api"])
        assert gw.route_request("/other/path", "client1") is None

    def test_rate_limits_client(self):
        gw = Gateway(GatewayConfig(
            routes=[Route("/api", make_backends())],
            rate_limit_capacity=2, rate_limit_refill=0.01
        ))
        gw.route_request("/api/x", "limited_client")
        gw.route_request("/api/x", "limited_client")
        result = gw.route_request("/api/x", "limited_client")
        assert result is None

    def test_increments_total_requests(self):
        gw = make_gateway()
        gw.route_request("/api/x", "c1")
        gw.route_request("/api/x", "c2")
        assert gw.total_requests == 2

    def test_longest_prefix_match(self):
        b1 = make_backends(1)
        b2 = make_backends(1)
        b2[0].url = "http://specific:9000"
        routes = [Route("/api", b1), Route("/api/v2", b2)]
        gw = Gateway(GatewayConfig(routes=routes))
        backend = gw.route_request("/api/v2/users", "c1")
        assert backend.url == "http://specific:9000"

    def test_marks_backend_unhealthy(self):
        backends = make_backends(2)
        gw = make_gateway(backends=backends)
        gw.mark_backend_health(backends[0].url, False)
        assert not backends[0].healthy

    def test_get_stats(self):
        gw = make_gateway()
        gw.route_request("/api/x", "c1")
        stats = gw.get_stats()
        assert stats["total_requests"] == 1
        assert "routes" in stats

    def test_record_response_updates_backend(self):
        gw = make_gateway()
        backend = gw.route_request("/api/x", "c1")
        gw.record_response(backend, success=True, latency_ms=42.0)
        assert backend.avg_latency_ms == 42.0


# ── HTTP Proxy ────────────────────────────────────────────────────────────────

def _mock_client(status: int = 200, body: bytes = b"ok") -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _timeout_client() -> httpx.AsyncClient:
    def handler(request: httpx.Request):
        raise httpx.TimeoutException("timed out", request=request)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _error_client() -> httpx.AsyncClient:
    def handler(request: httpx.Request):
        raise httpx.ConnectError("refused", request=request)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestGatewayProxy:
    @pytest.mark.asyncio
    async def test_proxy_returns_response(self):
        gw = make_gateway()
        resp, err = await gw.proxy_request("/api/users", _http_client=_mock_client(200, b"hello"))
        assert err == ""
        assert isinstance(resp, ProxyResponse)
        assert resp.status_code == 200
        assert resp.body == b"hello"

    @pytest.mark.asyncio
    async def test_proxy_records_backend_latency(self):
        gw = make_gateway()
        await gw.proxy_request("/api/x", _http_client=_mock_client())
        backend = gw.config.routes[0].backends[0]
        assert backend.avg_latency_ms >= 0

    @pytest.mark.asyncio
    async def test_proxy_rate_limited(self):
        gw = Gateway(GatewayConfig(
            routes=[Route("/api", make_backends())],
            rate_limit_capacity=1, rate_limit_refill=0.001,
        ))
        await gw.proxy_request("/api/x", _http_client=_mock_client())
        resp, err = await gw.proxy_request("/api/x", _http_client=_mock_client())
        assert resp is None
        assert err == "rate_limited"

    @pytest.mark.asyncio
    async def test_proxy_no_route(self):
        gw = make_gateway(prefixes=["/api"])
        resp, err = await gw.proxy_request("/other/path", _http_client=_mock_client())
        assert resp is None
        assert err == "no_route"

    @pytest.mark.asyncio
    async def test_proxy_no_healthy_backend(self):
        backends = make_backends(2)
        for b in backends:
            b.healthy = False
        gw = make_gateway(backends=backends)
        resp, err = await gw.proxy_request("/api/x", _http_client=_mock_client())
        assert resp is None
        assert err == "no_healthy_backend"

    @pytest.mark.asyncio
    async def test_proxy_timeout(self):
        gw = make_gateway()
        resp, err = await gw.proxy_request("/api/x", _http_client=_timeout_client())
        assert resp is None
        assert err == "timeout"

    @pytest.mark.asyncio
    async def test_proxy_connection_error(self):
        gw = make_gateway()
        resp, err = await gw.proxy_request("/api/x", _http_client=_error_client())
        assert resp is None
        assert err == "connection_error"

    @pytest.mark.asyncio
    async def test_proxy_5xx_opens_circuit(self):
        cb = CircuitBreaker(failure_threshold=1)
        backends = [Backend("http://svc:8000", circuit_breaker=cb)]
        gw = make_gateway(backends=backends)
        await gw.proxy_request("/api/x", _http_client=_mock_client(500, b"error"))
        assert backends[0].circuit_breaker.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_proxy_strip_prefix(self):
        seen_urls = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_urls.append(str(request.url))
            return httpx.Response(200, content=b"ok")

        backends = [Backend("http://upstream:8000")]
        route = Route("/api/v2", backends, strip_prefix=True)
        gw = Gateway(GatewayConfig(routes=[route]))
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await gw.proxy_request("/api/v2/users", _http_client=client)
        assert seen_urls[0].endswith("/users")

    @pytest.mark.asyncio
    async def test_proxy_increments_total_requests(self):
        gw = make_gateway()
        await gw.proxy_request("/api/x", _http_client=_mock_client())
        await gw.proxy_request("/api/x", _http_client=_mock_client())
        assert gw.total_requests == 2
