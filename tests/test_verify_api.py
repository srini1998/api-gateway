import importlib.util, json, os, threading, urllib.request, urllib.error
from http.server import HTTPServer
import pytest

ROOT = os.path.join(os.path.dirname(__file__), '..')


@pytest.fixture(scope='module')
def server():
    spec = importlib.util.spec_from_file_location('verify', os.path.join(ROOT, 'api', 'verify.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    srv = HTTPServer(('127.0.0.1', 0), mod.handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{srv.server_port}/api/verify', mod
    srv.shutdown()


def post(url, payload):
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_replays_golden_run(server):
    url, _ = server
    g = json.load(open(os.path.join(ROOT, 'tests', 'golden', 'kill-and-recover.json')))
    code, body = post(url, {'config': g['config'], 'arrivals': g['arrivals'], 'actions': g['actions']})
    assert code == 200 and body['digest'] == g['digest'] and body['requests'] == 300


def test_rejects_bad_shapes_and_oversized_runs(server):
    url, _ = server
    assert post(url, b'[1]')[0] == 400
    assert post(url, {'config': {'backends': [{'id': 'A'}]}, 'arrivals': [], 'actions': [{'t': 0, 'op': 'exec'}]})[0] == 400
    many = [{'id': i, 't': i, 'client': 'c', 'path': '/api'} for i in range(2001)]
    assert post(url, {'config': {'backends': [{'id': 'A'}]}, 'arrivals': many, 'actions': []})[0] == 400


def test_per_address_limit(server):
    url, mod = server
    mod._hits.clear()
    codes = [post(url, b'{}')[0] for _ in range(mod.PER_WINDOW + 2)]
    assert codes[-1] == 429
    mod._hits.clear()
