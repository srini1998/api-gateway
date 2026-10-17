# Distributed API Gateway

A failing backend should stop normal traffic while a single recovery probe checks its health.

Trip the circuit, allow one half-open request, and reject a second until completion. /api must not match /apiary.

See [review](docs/review.md) and [actual checks](docs/test-results.txt) for data, validation and limitations.

```sh
python -m pytest tests -q
```

No production-readiness or independent model-quality guarantee is claimed.
