# Read the existing bootstrap audit session (#407)

This read-only helper retrieves only `{job="cowlic-bootstrap"}` for **2026-10-12 10:03–10:16 UTC**, the fixed
window containing the session selected in #370. It does not start ingest, change permissions, or send new logs.

```sh
ingest/.venv/bin/python ingest/read_grafana_session.py
```

The project `.env` loader supplies `INGEST_GRAFANA_LOKI_URL`, `INGEST_GRAFANA_LOKI_USER` and the token. Set a
separate `INGEST_GRAFANA_READ_TOKEN` with **logs:read**, restricted to the project's existing stack. This leaves
`INGEST_GRAFANA_TOKEN` used by shipping unchanged. For an explicitly authorized diagnostic with the existing token,
the helper can use it when no read-token key exists; an invalid/empty read-token key never falls back to shipping.
Missing keys or inherited values shadowing the chosen `.env` credentials cause the CLI to refuse the request.

The helper reuses the shipper's HTTPS/host/header validation and no-redirect, no-proxy opener. It makes one GET with
a 20-second socket timeout and 30-second overall deadline, accepts at most 4 MiB and fewer than 1,000 entries,
and never retries. A full entry limit is refused as possibly truncated. HTTP failures report the status only;
network/parse failures report the class only. **401 does not establish whether the token is expired or lacks scope.**

The raw successful response is saved as a private 0600 file at
`~/.local/state/cowlic/release4-grafana-bootstrap-sample.json`; stdout contains counts/status only. Keep that file
local: it can contain private library names. Empty retained results are valid evidence, not proof that the original
session did not occur. Compare the export with the original local log before claiming completeness or phase timings.

Tests: from `ingest/`, run `.venv/bin/python -m unittest test_read_grafana_session test_ship_grafana`.

See [query authentication](https://grafana.com/docs/grafana-cloud/send-data/logs/authenticate-to-query-logs/),
[stack access policies](https://grafana.com/docs/grafana-cloud/platform/security-and-account-management/security-and-access/authentication-and-permissions/access-policies/create-access-policies/)
and [Loki HTTP API](https://grafana.com/docs/loki/latest/reference/loki-http-api/).
