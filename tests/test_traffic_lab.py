import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import pytest
from traffic_lab import (Mulberry32, fnv1a, LabConfig, BackendSpec, TrafficLab, replay, Breaker)

GOLDEN = os.path.join(os.path.dirname(__file__), 'golden')


def cfg(**kw):
    backends = kw.pop('backends', None) or [
        {'id': 'A', 'weight': 1, 'latency_ms': 40, 'jitter_ms': 10},
        {'id': 'B', 'weight': 1, 'latency_ms': 40, 'jitter_ms': 10},
        {'id': 'C', 'weight': 1, 'latency_ms': 40, 'jitter_ms': 10},
    ]
    base = {'backends': backends, 'rate_capacity': 1000, 'rate_refill_per_s': 1000}
    base.update(kw)
    return base


def burst(n, start=0.0, gap=10.0, clients=4, path='/api/orders'):
    return [{'id': i, 't': start + i * gap, 'client': f'c{i % clients}', 'path': path} for i in range(n)]


# ── primitives ───────────────────────────────────────────────────────────────

def test_mulberry32_reference_values():
    r = Mulberry32(7)
    vals = [r.next() for _ in range(3)]
    assert all(0 <= v < 1 for v in vals)
    assert vals == [Mulberry32(7).next()] + vals[1:]
    # frozen values; the TypeScript port asserts the same numbers
    assert Mulberry32(1).next() == 0.6270739405881613


def test_fnv1a_known_vector():
    assert fnv1a('') == 0x811C9DC5
    assert fnv1a('a') == 0xE40C292C


def test_smooth_weighted_round_robin_matches_nginx_sequence():
    lab = TrafficLab(LabConfig.from_dict(cfg(backends=[
        {'id': 'a', 'weight': 5}, {'id': 'b', 'weight': 1}, {'id': 'c', 'weight': 1}])))
    seq = [lab._pick('stable', [], 0).spec.id for _ in range(7)]
    assert seq == ['a', 'a', 'b', 'a', 'c', 'a', 'a']


def test_weight_zero_drains_backend():
    lab = replay(cfg(backends=[{'id': 'A', 'weight': 0}, {'id': 'B', 'weight': 1}]), burst(20), [])
    assert lab.backends['A'].requests == 0 and lab.backends['B'].requests == 20


# ── routing ──────────────────────────────────────────────────────────────────

def test_route_boundary_and_rate_limit():
    lab = replay(cfg(rate_capacity=3, rate_refill_per_s=0.001),
                 [{'id': 0, 't': 0, 'client': 'x', 'path': '/apiary'}] +
                 [{'id': i, 't': i, 'client': 'x', 'path': '/api/x'} for i in range(1, 6)], [])
    out = {r.id: r.outcome for r in lab.requests.values()}
    assert out[0] == 'no_route'          # /api must not match /apiary
    assert [out[i] for i in range(1, 6)].count('rate_limited') == 3  # bucket had 3 tokens, /apiary used one


def test_canary_split_is_sticky_per_client_and_bounded():
    backends = [{'id': 'S1'}, {'id': 'S2'}, {'id': 'K1', 'pool': 'canary'}]
    lab = replay(cfg(backends=backends, canary_percent=30), burst(400, clients=40), [])
    by_client = {}
    for r in lab.requests.values():
        by_client.setdefault(r.client, set()).add(r.pool)
    assert all(len(p) == 1 for p in by_client.values())
    share = sum(1 for p in by_client.values() if 'canary' in p) / len(by_client)
    assert 0.15 <= share <= 0.45
    assert all((fnv1a(c) % 100 < 30) == ('canary' in p) for c, p in by_client.items())
    none = replay(cfg(backends=backends, canary_percent=0), burst(50), [])
    assert all(r.pool == 'stable' for r in none.requests.values())


def test_canary_falls_back_to_stable_when_canary_is_open():
    backends = [{'id': 'S1'}, {'id': 'K1', 'pool': 'canary', 'down': True}]
    lab = replay(cfg(backends=backends, canary_percent=100, breaker_threshold=2, max_attempts=1),
                 burst(30, gap=20), [])
    assert lab.backends['K1'].breaker.state in ('open', 'half_open')
    assert any('fell back' in r.note and r.outcome == 'ok' for r in lab.requests.values())


# ── resilience ───────────────────────────────────────────────────────────────

def test_retry_moves_to_a_different_backend_and_succeeds():
    lab = replay(cfg(backends=[{'id': 'A', 'down': True}, {'id': 'B'}], breaker_threshold=100,
                     retry_budget_min=100),
                 burst(10, gap=100), [])
    for r in lab.requests.values():
        assert r.outcome == 'ok'
        names = [a.backend for a in r.attempts]
        assert len(names) == len(set(names))


def test_backoff_is_full_jitter_and_capped():
    lab = replay(cfg(backends=[{'id': 'A', 'error_rate': 1}, {'id': 'B', 'error_rate': 1}, {'id': 'C', 'error_rate': 1}],
                     max_attempts=3, backoff_base_ms=100, backoff_cap_ms=150, retry_budget_min=100,
                     breaker_threshold=100), burst(30, gap=1000), [])
    backoffs = [(i, a.backoff_ms) for r in lab.requests.values() for i, a in enumerate(r.attempts[:-1])]
    assert backoffs
    for i, b in backoffs:
        assert 0 <= b < min(150, 100 * 2 ** i)
    assert all(r.outcome == 'failed' and len(r.attempts) == 3 for r in lab.requests.values())


def test_retry_budget_prevents_retry_storm():
    lab = replay(cfg(backends=[{'id': 'A', 'error_rate': 1}, {'id': 'B', 'error_rate': 1}],
                     retry_budget_min=2, retry_budget_ratio=0.1, breaker_threshold=1000),
                 burst(100, gap=50), [])
    assert lab.retries_used <= 2 + 0.1 * 100
    assert lab.retries_denied > 0
    assert any(r.note == 'retry budget exhausted' for r in lab.requests.values())


def test_breaker_opens_then_short_circuits_without_touching_backend():
    lab = replay(cfg(backends=[{'id': 'A', 'down': True}], breaker_threshold=3, max_attempts=1,
                     breaker_recovery_ms=60000), burst(20, gap=10), [])
    assert lab.backends['A'].requests == 3
    assert lab.backends['A'].breaker.state == 'open'
    assert sum(r.outcome == 'short_circuited' for r in lab.requests.values()) == 17


def test_half_open_allows_one_concurrent_probe_then_closes():
    conf = cfg(backends=[{'id': 'A', 'down': True, 'latency_ms': 200, 'jitter_ms': 0}], breaker_threshold=2,
               max_attempts=1, breaker_recovery_ms=1000, timeout_ms=500)
    lab = TrafficLab(LabConfig.from_dict(conf))
    lab.add_arrivals(burst(2, gap=5))
    lab.run_until(100)
    assert lab.backends['A'].breaker.state == 'open'
    lab.apply({'op': 'backend', 'id': 'A', 'patch': {'down': False}})
    # three overlapping requests after recovery: only the first may probe (200 ms in flight)
    lab.add_arrivals([{'id': 10 + i, 't': 1200 + i * 20, 'client': 'c', 'path': '/api'} for i in range(3)])
    lab.run_all()
    r = lab.requests
    assert r[10].attempts[0].probe and r[10].outcome == 'ok'
    assert r[11].outcome == 'short_circuited' and r[12].outcome == 'short_circuited'
    states = [t['to'] for t in lab.backends['A'].breaker.transitions]
    assert states == ['open', 'half_open', 'closed']


def test_failed_probe_reopens():
    b = Breaker(1, 1000, 100)
    b.on_failure(0, False)
    assert b.state == 'open' and not b.can_pass(50)
    assert b.can_pass(100) and b.state == 'half_open'
    b.probe_in_flight = True
    assert not b.can_pass(101)
    b.on_failure(150, True)
    assert b.state == 'open' and b.opened_at == 150


def test_timeout_is_enforced_per_try():
    lab = replay(cfg(backends=[{'id': 'A', 'latency_ms': 900, 'jitter_ms': 0}], timeout_ms=300, max_attempts=1),
                 burst(1), [])
    r = lab.requests[0]
    assert r.outcome == 'failed' and r.status == 504 and r.attempts[0].end - r.attempts[0].start == 300


def test_kill_mid_burst_changes_outcomes_and_is_reproducible():
    conf = cfg(breaker_threshold=3)
    arrivals = burst(120, gap=25, clients=6)
    calm = replay(conf, arrivals, [])
    kill = [{'t': 1000, 'op': 'backend', 'id': 'B', 'patch': {'down': True}}]
    a = replay(conf, arrivals, kill)
    b = replay(conf, arrivals, kill)
    assert a.digest() == b.digest() != calm.digest()
    assert a.backends['B'].breaker.transitions[0]['to'] == 'open'
    assert sum(r.outcome == 'ok' for r in a.requests.values()) >= 110  # retries hide most of the outage


def test_validation_rejects_bad_input():
    with pytest.raises(ValueError):
        LabConfig.from_dict({'backends': []})
    with pytest.raises(ValueError):
        LabConfig.from_dict(cfg(max_attempts=9))
    with pytest.raises(ValueError):
        replay(cfg(), burst(2001, gap=1), [])
    with pytest.raises(ValueError):
        replay(cfg(), burst(3), [{'t': 5, 'op': 'shell', 'cmd': 'rm'}])
    with pytest.raises(ValueError):
        replay(cfg(), burst(3), [{'t': 5, 'op': 'backend', 'id': 'A', 'patch': {'error_rate': 5}}])
    with pytest.raises(ValueError):
        replay(cfg(), burst(3), [{'t': 5, 'op': 'backend', 'id': 'Z', 'patch': {}}])


# ── golden traces for the TypeScript port ────────────────────────────────────

SCENARIOS = {
    'steady': (cfg(), burst(60, gap=30, clients=5), []),
    'weighted-canary': (cfg(backends=[{'id': 'S1', 'weight': 3}, {'id': 'S2', 'weight': 1},
                                      {'id': 'K1', 'pool': 'canary', 'error_rate': 0.3}],
                            canary_percent=40), burst(200, gap=7, clients=23), []),
    'kill-and-recover': (cfg(breaker_threshold=3, breaker_recovery_ms=800),
                         burst(300, gap=12, clients=9),
                         [{'t': 600, 'op': 'backend', 'id': 'C', 'patch': {'down': True}},
                          {'t': 1500, 'op': 'backend', 'id': 'A', 'patch': {'latency_ms': 400}},
                          {'t': 2600, 'op': 'backend', 'id': 'C', 'patch': {'down': False}},
                          {'t': 2700, 'op': 'canary', 'percent': 0}]),
    'storm': (cfg(backends=[{'id': 'A', 'error_rate': 0.7}, {'id': 'B', 'error_rate': 0.7}],
                  rate_capacity=15, rate_refill_per_s=5, retry_budget_min=3, retry_budget_ratio=0.15),
              [{'id': i, 't': (i // 40) * 900 + (i % 40), 'client': f'c{i % 3}',
                'path': '/apiary' if i % 50 == 0 else '/api/search'} for i in range(240)], []),
}


def build_golden(name):
    conf, arrivals, actions = SCENARIOS[name]
    lab = replay(json.loads(json.dumps(conf)), arrivals, actions)
    return {'name': name, 'config': conf, 'arrivals': arrivals, 'actions': actions,
            'compact': lab.compact(), 'digest': lab.digest(), 'summary': lab.summary(),
            'rng_seed1': [Mulberry32(1).next()]}


@pytest.mark.parametrize('name', sorted(SCENARIOS))
def test_golden_traces_are_current(name):
    os.makedirs(GOLDEN, exist_ok=True)
    path = os.path.join(GOLDEN, f'{name}.json')
    data = build_golden(name)
    if os.environ.get('UPDATE_GOLDEN') or not os.path.exists(path):
        with open(path, 'w') as f:
            json.dump(data, f, indent=1)
    with open(path) as f:
        saved = json.load(f)
    assert saved['digest'] == data['digest']
    assert saved['compact'] == json.loads(json.dumps(data['compact']))


def test_interleaved_live_session_equals_one_shot_replay():
    """The website adds bursts while the clock runs; the recorded inputs must replay identically."""
    conf = cfg(breaker_threshold=3)
    lab = TrafficLab(LabConfig.from_dict(conf))
    arrivals, actions, t = [], [], 0.0
    nid = 0
    for step in range(6):
        batch = [{'id': nid + i, 't': t + 1 + i * 3.0, 'client': f'c{i % 5}', 'path': '/api'} for i in range(40)]
        nid += 40
        lab.add_arrivals(batch)
        arrivals += batch
        t += 333.3
        lab.run_until(t)
        if step == 2:
            act = {'t': lab.now, 'op': 'backend', 'id': 'A', 'patch': {'down': True}}
            lab.apply(act)
            actions.append(act)
    lab.run_all()
    assert lab.digest() == replay(conf, arrivals, actions).digest()
