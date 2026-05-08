# AGENTS.md

## Review guidelines

- Treat leaked secrets, API keys, broker credentials, JWT/session secrets, cloud keys, and `.env` exposure as P0.
- Treat any change that can place, modify, cancel, or exit live orders as P0/P1 unless tests prove safe behavior.
- Verify LIVE mode cannot fall back to unsafe in-memory state, mock order clients, or permissive defaults.
- Verify Docker, OCI VM, nginx, FastAPI, Postgres, and environment wiring remain consistent.
- Flag broken imports, dead routes, stale config, missing migrations, and unwired modules.
- Flag missing tests for risk manager, order lifecycle, broker adapter, strategy triggers, and deployment gates.
- Do not suggest exposing secrets in logs, docs, GitHub Actions, Docker compose, or screenshots.
- Review documentation updates for accuracy; stale deployment docs should be flagged.