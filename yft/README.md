# YFT component

This folder contains the sanitized YFT source snapshot: page, client,
service worker, isolated Python server, its backend helper module, diagnostic
tools, local launch scripts, and deployment reference files.

- Public test URL: https://code2it.com/yft/
- Local URL: http://127.0.0.1:8183/yft/
- The public page returned HTTP 200 on 2026-10-24.

No API keys, OAuth secrets, cookies, account database, Python environment,
cache, or downloaded media are included here. Configure private runtime
material outside Git. The included Nginx and systemd files are snapshots; check
them against the target host before deploying.

The local PowerShell launch scripts use an external data root by default at
`../../work/yft-lab`. That folder must provide a virtual environment and
private `ytapi` runtime copy. This component folder alone is not a complete
runtime.

See the repository-level [README](../README.md) for features and recorded
verification, and [AI_HANDOFF.md](../AI_HANDOFF.md) for the route contracts,
data flow, configuration, invariants, and next-agent checklist.
