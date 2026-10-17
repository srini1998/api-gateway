# Engineering review / October 17, 2026

## problem
A failing backend should stop normal traffic while a single recovery probe checks its health.

## example
Trip the circuit, allow one half-open request, and reject a second until completion. /api must not match /apiary.

## dataSource
Authored requests and injected local transport failures.

## verification
32 passing checks, including asynchronous proxy errors, one outstanding recovery probe and route boundaries.

## limitations
In-process state. MockTransport checks are not a production load or multi-process benchmark.

## Reproduce
Python 3.12, free project requirements and pytest: `python -m pytest tests -q`. Actual output: docs/test-results.txt. No paid API calls used. Optional local generation needs Ollama/qwen2.5:3b.
