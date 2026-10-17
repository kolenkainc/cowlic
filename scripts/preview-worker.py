#!/usr/bin/env python3
"""Explicit private preview provisioning, snapshot refresh, deployment and checks. Never writes production."""
import argparse
from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ingest"))
import tg_check

NAME = "cowlic-preview"
UUID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")
MAX_DUMP = 128 * 1024 * 1024


class PreviewError(Exception):
    pass


def require(ok, message):
    if not ok:
        raise PreviewError(message)


def private_dir(path):
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid(), "unsafe state directory")
    path.chmod(0o700)


def save(path, value):
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(value, out)
            out.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load(path):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_mode & 0o077 == 0,
            "unsafe private state file")
    return json.loads(path.read_text())


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def interrupted(signum, _frame):
    raise SystemExit(128 + signum)


def fingerprint(path):
    require(path.stat().st_size <= MAX_DUMP, "snapshot exceeds local cap")
    deadline = time.monotonic() + 30
    with closing(sqlite3.connect(":memory:")) as db:
        db.set_authorizer(lambda action, *_: sqlite3.SQLITE_DENY if action in
                          (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH) else sqlite3.SQLITE_OK)
        db.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
        db.executescript(path.read_text())
        names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                 if not row[0].startswith(("sqlite_", "_cf_"))]
        require({"artists", "albums", "tracks", "d1_migrations"} <= set(names), "snapshot missing library or migration history")
        result = {}
        for name in names:
            rows = sorted(json.dumps(row, ensure_ascii=True, separators=(",", ":"))
                          for row in db.execute(f"SELECT * FROM {quote(name)}"))
            schema = list(db.execute(f"PRAGMA table_info({quote(name)})"))
            foreign_keys = list(db.execute(f"PRAGMA foreign_key_list({quote(name)})"))
            indexes = list(db.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name=? ORDER BY name", (name,)))
            result[name] = {"rows": len(rows), "hash": hashlib.sha256("\n".join(rows).encode()).hexdigest(),
                            "schema": schema, "foreign_keys": foreign_keys, "indexes": indexes}
        require(not list(db.execute("PRAGMA foreign_key_check")), "snapshot has foreign-key violations")
        return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class Preview:
    def __init__(self, directory):
        self.directory = directory
        self.manifest_path = directory / "manifest.json"
        self.config = directory / "wrangler.preview.toml"
        self.deadline = time.monotonic() + 1200
        tg_check.load_env()
        self.account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
        self.token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
        self.d1_token = os.environ.get("INGEST_CLOUDFLARE_API_TOKEN", "")
        require(re.fullmatch(r"[a-f0-9]{32}", self.account) and self.token and self.d1_token, "missing account or local Cloudflare tokens")
        source = tomllib.loads((ROOT / "worker/wrangler.local.toml").read_text())
        bindings = source.get("d1_databases", [])
        require(len(bindings) == 1 and bindings[0].get("binding") == "DB", "ambiguous production binding")
        self.source = bindings[0]["database_id"]
        self.source_name = bindings[0]["database_name"]
        self.source_worker = source["name"]
        require(UUID.fullmatch(self.source) and self.source_name.casefold() != NAME and self.source_worker.casefold() != NAME, "invalid production identity")
        require(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", self.source_name) and
                re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", self.source_worker), "invalid production resource name")
        self.manifest = load(self.manifest_path) if self.manifest_path.exists() else None

    def timeout(self, maximum):
        remaining = self.deadline - time.monotonic()
        require(remaining > 0, "operation budget exhausted")
        return min(maximum, remaining)

    def api(self, suffix, data=None, d1=False):
        # Only fixed Cloudflare control-plane paths, no redirects or raw error bodies.
        req = urllib.request.Request(f"https://api.cloudflare.com/client/v4/accounts/{self.account}/{suffix}",
                                     data=json.dumps(data).encode() if data is not None else None,
                                     headers={"Authorization": "Bearer " + (self.d1_token if d1 else self.token),
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=self.timeout(30)) as response:
                result = json.loads(response.read(16 * 1024 * 1024))
        except urllib.error.HTTPError as error:
            raise PreviewError(f"Cloudflare API HTTP {error.code}; check local account permissions") from None
        except (urllib.error.URLError, ValueError):
            raise PreviewError("Cloudflare request failed; check local account permissions") from None
        require(result.get("success") is True, "Cloudflare operation rejected")
        return result["result"]

    def databases(self):
        return self.api("d1/database?per_page=100", d1=True)

    def verify_source(self):
        bindings = self.api(f"workers/scripts/{self.source_worker}/settings")["bindings"]
        databases = [b for b in bindings if b.get("type") == "d1" and b.get("name") == "DB"]
        require(len(databases) == 1 and databases[0].get("id") == self.source,
                "local source does not match the deployed production Worker binding")

    def guard(self):
        m = self.manifest
        require(m and m.get("account") == self.account and m.get("source") == self.source and
                m.get("worker") == NAME and m.get("database_name") == NAME and
                UUID.fullmatch(m.get("database_id", "")) and m["database_id"] != self.source,
                "preview identity missing or overlaps production")
        matches = [d for d in self.databases() if d["uuid"] == m["database_id"]]
        require(len(matches) == 1 and matches[0]["name"] == NAME, "preview target does not match account inventory")
        return m["database_id"]

    def query(self, sql, source=False):
        target = self.source if source else self.guard()
        # Production has exactly one permitted SQL shape: the read-only isolation probe.
        if source:
            require(re.fullmatch(r"SELECT name FROM sqlite_master WHERE name='cowlic_preview_probe_[a-f0-9]+'", sql),
                    "production query is not the allowed read-only probe")
        results = self.api(f"d1/database/{target}/query", {"sql": sql}, d1=True)
        require(results and all(r.get("success") is True for r in results), "D1 query failed")
        return results[-1]["results"]

    def command(self, args, token=None, data=None, maximum=600):
        env = {k: os.environ[k] for k in ("PATH", "HOME", "XDG_CONFIG_HOME") if k in os.environ}
        env.update(CLOUDFLARE_API_TOKEN=token or self.d1_token, CLOUDFLARE_ACCOUNT_ID=self.account,
                   CI="true", WRANGLER_SEND_METRICS="false")
        # Raw output stays in a private diagnostic file. Never relay SQL, identifiers or request URLs to stdout.
        fd = os.open(self.directory / "last-command.log", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as log:
            process = subprocess.Popen(["pnpm", "exec", "wrangler", *args], cwd=ROOT / "worker", env=env,
                                       stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                                       stdout=log, stderr=log, start_new_session=True)
            try:
                process.communicate(data, timeout=self.timeout(maximum))
            except BaseException:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # The child already exited; it still must be reaped below.
                process.wait()
                raise
        require(process.returncode == 0, "Wrangler command failed; diagnostics remain private")

    def write_config(self):
        self.verify_source()
        self.guard()
        text = (ROOT / "worker/wrangler.preview.toml").read_text()
        text = text.replace('"SET_BY_PRIVATE_PREVIEW_TOOL"', json.dumps(self.manifest["database_id"]))
        text = text.replace('"src/index.ts"', json.dumps(str(ROOT / "worker/src/index.ts")))
        text = text.replace('"migrations"', json.dumps(str(ROOT / "worker/migrations")))
        parsed = tomllib.loads(text)
        require(parsed["name"] == NAME and parsed.get("routes") == [] and parsed.get("workers_dev") is True and
                parsed.get("preview_urls") is False and not parsed.get("vars") and not parsed.get("env") and
                parsed["observability"].get("redact_query_string") is True and
                parsed["observability"]["logs"]["invocation_logs"] is False,
                "unsafe preview Worker configuration")
        require(len(parsed["d1_databases"]) == 1 and parsed["d1_databases"][0] == {
            "binding": "DB", "database_name": NAME, "database_id": self.guard(),
            "migrations_dir": str(ROOT / "worker/migrations")}, "unsafe preview D1 configuration")
        fd = os.open(self.config, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write(text)

    def init(self):
        require(self.manifest is None, "preview already initialized; use refresh or deploy")
        require(os.environ.get("TG_BOT_TOKEN"), "local Telegram read credential missing")
        databases = self.databases()
        require(len(databases) < 10 and not any(d["name"] == NAME for d in databases), "preview name exists or free-plan database cap reached")
        require(any(d["uuid"] == self.source and d["name"] == self.source_name for d in databases), "production source does not match account inventory")
        self.verify_source()
        workers = self.api("workers/scripts")
        require(not any(w["id"] == NAME for w in workers), "preview Worker name already exists; refusing to overwrite it")
        info = self.api(f"d1/database/{self.source}", d1=True)
        require(isinstance(info.get("file_size"), int) and info["file_size"] < 500_000_000,
                "source exceeds preview free-plan capacity or size is unavailable")
        self.command(["--version"], maximum=15)
        created = self.api("d1/database", {"name": NAME}, d1=True)
        require(created.get("name") == NAME and UUID.fullmatch(created.get("uuid", "")) and created["uuid"] != self.source,
                "unexpected created database identity")
        self.manifest = {"account": self.account, "source": self.source, "database_id": created["uuid"],
                         "database_name": NAME, "worker": NAME, "ready": False, "snapshot_verified": False}
        save(self.manifest_path, self.manifest)
        save(self.directory / "access.json", {"username": "preview", "password": secrets.token_urlsafe(32)})
        self.refresh()
        self.deploy()

    def refresh(self):
        self.write_config()
        self.manifest.update(ready=False, snapshot_verified=False)
        save(self.manifest_path, self.manifest)
        with tempfile.TemporaryDirectory(dir=self.directory) as temporary:
            original, copied = Path(temporary) / "source.sql", Path(temporary) / "copied.sql"
            export_started = datetime.now(timezone.utc).isoformat()
            self.command(["d1", "export", self.source_name, "--remote", "--output", str(original),
                          "-c", str(ROOT / "worker/wrangler.local.toml")], maximum=300)
            captured_at = datetime.now(timezone.utc).isoformat()
            expected = fingerprint(original)
            tables = self.query("SELECT name FROM sqlite_master WHERE type='table'")
            names = [r["name"] for r in tables if not r["name"].startswith(("sqlite_", "_cf_"))]
            if names:
                # Clear children before parents, independent of sqlite_master order.
                dependencies = {n: {r["table"] for r in self.query(f"PRAGMA foreign_key_list({quote(n)})")} for n in names}
                order = []
                while dependencies:
                    referenced = set().union(*dependencies.values())
                    children = sorted(set(dependencies) - referenced)
                    require(children, "preview FK cycle needs an explicit reset plan")
                    order.extend(children)
                    for n in children:
                        del dependencies[n]
                self.query("PRAGMA defer_foreign_keys=TRUE; " + " ".join(f"DROP TABLE {quote(n)};" for n in order))
            self.guard()
            self.command(["d1", "execute", NAME, "--remote", "--file", str(original), "-c", str(self.config)])
            self.command(["d1", "export", NAME, "--remote", "--output", str(copied), "-c", str(self.config)], maximum=300)
            require(fingerprint(copied) == expected, "preview snapshot or migration ledger differs from captured source")
            os.replace(original, self.directory / "snapshot.sql")
        self.isolation_probe()
        self.manifest.update(snapshot_verified=True, snapshot_export_started_at=export_started,
                             snapshot_at=captured_at, verified_at=datetime.now(timezone.utc).isoformat(), snapshot=expected)
        save(self.manifest_path, self.manifest)
        print(f"preview-worker: copied tables={len(expected)} rows={sum(t['rows'] for t in expected.values())}")

    def isolation_probe(self):
        name = "cowlic_preview_probe_" + secrets.token_hex(8)
        sql = f"SELECT name FROM sqlite_master WHERE name='{name}'"
        require(not self.query(sql, source=True), "isolation probe exists in source")
        try:
            self.query(f"CREATE TABLE {quote(name)} (value INTEGER); INSERT INTO {quote(name)} VALUES (42)")
            require(self.query(f"SELECT value FROM {quote(name)}") == [{"value": 42}], "preview probe write not visible")
            require(not self.query(sql, source=True), "preview probe reached production")
        finally:
            self.query(f"DROP TABLE IF EXISTS {quote(name)}")

    def deploy(self):
        self.write_config()
        require(self.manifest.get("snapshot_verified") is True, "refresh and verify the snapshot before deploying")
        dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all", "--", "worker", "package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml"], cwd=ROOT)
        require(not dirty, "commit the candidate Worker inputs before preview deployment")
        self.manifest["ready"] = False
        self.manifest["deployed_commit"] = None
        save(self.manifest_path, self.manifest)
        candidate = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        self.command(["d1", "migrations", "apply", NAME, "--remote", "-c", str(self.config)])
        self.command(["deploy", "-c", str(self.config), "--tag", candidate[:7], "--message", f"preview candidate {candidate}"],
                     token=self.token, maximum=180)
        access = load(self.directory / "access.json")
        bot = os.environ.get("TG_BOT_TOKEN", "")
        require(bot, "local Telegram read credential missing")
        self.command(["secret", "bulk", "-c", str(self.config)], token=self.token, maximum=120,
                     data=json.dumps({"TG_BOT_TOKEN": bot, "SUBSONIC_USER": access["username"], "SUBSONIC_PASSWORD": access["password"]}).encode())
        subdomain = self.api("workers/subdomain")["subdomain"]
        require(re.fullmatch(r"[a-z0-9-]+", subdomain), "unexpected workers.dev subdomain")
        access["url"] = f"https://{NAME}.{subdomain}.workers.dev"
        save(self.directory / "access.json", access)
        self.manifest["deployed_commit"] = candidate
        save(self.manifest_path, self.manifest)
        self.check()
        self.manifest.update(ready=True, tested_commit=candidate)
        save(self.manifest_path, self.manifest)
        print("preview-worker: ready; connection details are in private access.json")

    def check(self):
        self.verify_source()
        self.guard()
        require(self.manifest.get("snapshot_verified") is True and
                re.fullmatch(r"[a-f0-9]{40}", self.manifest.get("deployed_commit") or ""),
                "deploy a verified snapshot before confirming readiness")
        access = load(self.directory / "access.json")
        subdomain = self.api("workers/subdomain")["subdomain"]
        require(re.fullmatch(r"[a-z0-9-]+", subdomain) and access.get("url") == f"https://{NAME}.{subdomain}.workers.dev",
                "preview endpoint does not match this account")

        def request(method, params=None, byte_range=None, authenticate=True):
            values = {"f": "json", "v": "1.16.1", "c": "preview-check", **(params or {})}
            if authenticate:
                salt = secrets.token_hex(8)
                values.update(u=access["username"], s=salt, t=hashlib.md5((access["password"] + salt).encode()).hexdigest())
            req = urllib.request.Request(access["url"] + "/rest/" + method + "?" + urllib.parse.urlencode(values),
                                         headers={"User-Agent": "cowlic-preview-check/1.0 (+https://github.com/kolenkainc/cowlic)",
                                                  **({"Range": byte_range} if byte_range else {})})
            try:
                with urllib.request.build_opener(NoRedirect()).open(req, timeout=self.timeout(30)) as response:
                    return response.status, response.headers, response.read(2 * 1024 * 1024)
            except urllib.error.HTTPError as error:
                headers = error.headers
                layer = "cloudflare" if "cf-ray" in headers or "cloudflare" in headers.get("server", "").lower() else "unknown"
                if "x-mitmproxy-blocked-reason" in headers or "envoy" in headers.get("server", "").lower():
                    layer = "gateway"
                reason = headers.get("x-mitmproxy-blocked-reason", "")
                known_reason = reason if reason in ("ROBOTS_DENIED", "DOMAIN_NOT_ALLOWED", "INTERNAL") else "unspecified"
                # Extract only a numeric provider code, never return the error page or its identifiers.
                with error:
                    page = error.read(65536).decode("utf-8", errors="replace")
                code = re.search(r"(?:error code:\s*|error-code\">)(\d{4})\b", page, re.IGNORECASE)
                provider_code = code.group(1) if code else "unspecified"
                challenge = headers.get("cf-mitigated") == "challenge"
                raise PreviewError(f"preview HTTP check failed: {method} HTTP {error.code} layer={layer} "
                                   f"reason={known_reason} provider_code={provider_code} challenge={challenge}") from None
            except urllib.error.URLError as error:
                raise PreviewError(f"preview HTTP check failed: {method} transport {type(error.reason).__name__}") from None

        def api(method, params=None, authenticate=True):
            status, _headers, body = request(method, params, authenticate=authenticate)
            require(status == 200, "preview HTTP status failed")
            response = json.loads(body)["subsonic-response"]
            require(response["status"] == ("ok" if authenticate else "failed"), "preview authentication or library response failed")
            return response

        require(api("getArtists", authenticate=False).get("error", {}).get("code") == 40, "preview library is not protected")
        api("ping")
        api("getArtists")
        songs = self.query("SELECT id,album_id,size_bytes FROM tracks WHERE size_bytes > 2048 ORDER BY tg_message_id DESC LIMIT 1")
        require(songs, "snapshot has no playable sample")
        song = songs[0]
        require(api("getSong", {"id": song["id"]})["song"]["id"] == song["id"], "preview song differs")
        api("getAlbum", {"id": song["album_id"]})
        covers = self.query("SELECT id FROM albums WHERE cover_file_id IS NOT NULL LIMIT 1")
        require(covers, "snapshot has no cover sample")
        status, headers, body = request("getCoverArt", {"id": covers[0]["id"]})
        require(status == 200 and headers.get_content_type().startswith("image/") and body, "preview cover check failed")
        for start in (0, 1024):
            status, headers, body = request("stream", {"id": song["id"]}, f"bytes={start}-{start+127}")
            require(status == 206 and len(body) == 128 and
                    headers.get("Content-Range") == f"bytes {start}-{start+127}/{song['size_bytes']}", "preview stream/seek check failed")
        print("preview-worker: authenticated library/cover/stream/seek checks passed")

    def status(self):
        self.verify_source()
        self.guard()
        endpoint = self.api(f"workers/scripts/{NAME}/subdomain")
        print("preview-worker: " + json.dumps({"ready": self.manifest.get("ready") is True,
              "snapshot_verified": self.manifest.get("snapshot_verified") is True,
              "endpoint_enabled": endpoint.get("enabled"), "version_preview_urls": endpoint.get("previews_enabled")}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "refresh", "deploy", "check", "status"))
    args = parser.parse_args()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    os.umask(0o077)
    base = Path(os.environ.get("COWLIC_STATE_DIR") or str(Path(os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")) / "cowlic"))
    try:
        require(not base.resolve().is_relative_to(ROOT.resolve()), "private state must be outside the repository")
        private_dir(base)
        directory = base / "preview"
        private_dir(directory)
        fd = os.open(directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            manifest_path = directory / "manifest.json"
            if args.action != "status" and manifest_path.exists():
                previous = load(manifest_path)
                previous["ready"] = False
                save(manifest_path, previous)
            preview = Preview(directory)
            getattr(preview, args.action)()
            if args.action == "check":
                preview.manifest["ready"] = True
                preview.manifest["tested_commit"] = preview.manifest["deployed_commit"]
                save(preview.manifest_path, preview.manifest)
        return 0
    except PreviewError as error:
        print(f"preview-worker: {error}")
    except Exception as error:
        # Only a class, never provider errors, JSON payloads, URLs, SQL or environment values.
        print(f"preview-worker: failed ({type(error).__name__}); preview is not confirmed ready")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
