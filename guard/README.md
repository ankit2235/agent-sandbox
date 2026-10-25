# guard

The guard library (v0.2): policies, hard rules, decision backends, hash-chained decision
records, a decorator and an MCP proxy. Domain-neutral; nothing here imports a world.

| Path | What |
| --- | --- |
| `src/guard/` | the Python SDK (`agent-sandbox-bench-guard`, command `sandbox-guard`) |
| `src/guard/policies/<world>/` | policy packs (data) |
| `src/guard/backends/` | decision backends and their configs |
| `src/guard/schemas/` | the JSON Schemas both SDKs share |
| `typescript/` | the TypeScript SDK |
| `test-vectors/` | the vectors both SDKs pass, written by `scripts/guard_vectors.py` |

How it works and how to run it: [docs/site/guard.md](../docs/site/guard.md).
