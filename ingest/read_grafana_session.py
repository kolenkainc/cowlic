#!/usr/bin/env python3
"""Read only the existing #407 bootstrap session; credentials and raw logs stay private.

Uses the project's existing .env loader, credential/host validation and no-redirect,
no-proxy HTTPS opener. One GET, no retries, no scope changes or ingest runs.
"""
import base64
import json
import os
from pathlib import Path
import queue
import threading
import urllib.error
import urllib.parse
import urllib.request

import ingest
import ship_grafana
import tg_check

START = "2026-10-12T10:03:00Z"
END = "2026-10-12T10:16:00Z"
QUERY = '{job="cowlic-bootstrap"}'
LIMIT = 1000
MAX_BYTES = 4 * 1024 * 1024
OUTPUT_NAME = "release4-grafana-bootstrap-sample.json"


class ReadError(Exception):
    """Only class/status messages belong here; never raw remote error text."""


def read_session(env, opener=None):
    token_key = "INGEST_GRAFANA_READ_TOKEN" if "INGEST_GRAFANA_READ_TOKEN" in env else "INGEST_GRAFANA_TOKEN"
    config = {key: env.get(key, "") for key in ("INGEST_GRAFANA_LOKI_URL", "INGEST_GRAFANA_LOKI_USER")}
    config["INGEST_GRAFANA_TOKEN"] = env.get(token_key, "")
    push_url, user, token = ship_grafana.read_config(config)
    base = urllib.parse.urlsplit(push_url)
    params = urllib.parse.urlencode({"query": QUERY, "start": START, "end": END,
                                    "limit": LIMIT, "direction": "forward"})
    url = urllib.parse.urlunsplit((base.scheme, base.netloc, "/loki/api/v1/query_range", params, ""))
    auth = base64.b64encode(f"{user}:{token}".encode()).decode()
    request = urllib.request.Request(url, method="GET", headers={
        "Authorization": "Basic " + auth, "Accept": "application/json",
        "User-Agent": "cowlic-existing-session-audit"})
    opener = opener or ship_grafana.make_opener()
    result = queue.Queue(maxsize=1)

    def fetch():
        try:
            with opener.open(request, timeout=20) as response:
                if response.status != 200:
                    raise ReadError(f"HTTP {response.status}")
                body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise ReadError("response too large")
                result.put((body, None))
        except urllib.error.HTTPError as error:
            error.close()
            result.put((None, f"HTTP {error.code}"))
        except Exception as error:
            result.put((None, str(error) if isinstance(error, ReadError) else type(error).__name__))

    thread = threading.Thread(target=fetch, daemon=True)
    thread.start()
    thread.join(30)
    if thread.is_alive():
        raise ReadError("deadline")
    body, error = result.get_nowait()
    if error:
        raise ReadError(error)
    data = ship_grafana.strict_json(body)
    if data.get("status") != "success" or data.get("data", {}).get("resultType") != "streams":
        raise ReadError("unexpected response")
    streams = data["data"]["result"]
    if not isinstance(streams, list):
        raise ReadError("unexpected streams")
    count = 0
    for stream in streams:
        if stream.get("stream", {}).get("job") != "cowlic-bootstrap":
            raise ReadError("unexpected stream")
        for entry in stream["values"]:
            if not isinstance(entry, list) or len(entry) < 2 or not all(isinstance(v, str) for v in entry[:2]):
                raise ReadError("unexpected entry")
            int(entry[0])
            count += 1
    if count >= LIMIT:
        raise ReadError("result may be truncated")
    return data, count


def main():
    try:
        tg_check.load_env()
        token_key = "INGEST_GRAFANA_READ_TOKEN" if "INGEST_GRAFANA_READ_TOKEN" in os.environ else "INGEST_GRAFANA_TOKEN"
        required = {"INGEST_GRAFANA_LOKI_URL", "INGEST_GRAFANA_LOKI_USER", token_key}
        if not required <= tg_check.ENV_FROM_FILE:
            raise ReadError("project .env keys missing or shadowed; no inherited credential substitution")
        data, count = read_session(os.environ)
        directory = Path.home() / ".local/state/cowlic"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        ingest.atomic_write_json(directory / OUTPUT_NAME, data)
        print(json.dumps({"read": "ok", "lines": count, "private_export_saved": True}))
        return 0
    except Exception as error:
        safe = str(error) if isinstance(error, ReadError) else type(error).__name__
        print(json.dumps({"read": "stopped", "reason": safe}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
