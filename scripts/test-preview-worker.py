"""Offline preview isolation, populated snapshot, ledger and failure checks. Synthetic data only."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import tempfile
import time
import unittest
from email.message import Message
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("preview_worker", ROOT / "scripts/preview-worker.py")
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)
SOURCE = "11111111-1111-1111-1111-111111111111"
TARGET = "22222222-2222-2222-2222-222222222222"
ACCOUNT = "a" * 32


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "worker/src").mkdir(parents=True)
        (self.root / "worker/migrations").mkdir()
        (self.root / "worker/wrangler.local.toml").write_text(
            f'name="cowlic"\n[[d1_databases]]\nbinding="DB"\ndatabase_name="cowlic"\ndatabase_id="{SOURCE}"\n')
        (self.root / "worker/wrangler.preview.toml").write_text((ROOT / "worker/wrangler.preview.toml").read_text())
        self.state = self.root / "private"
        preview.private_dir(self.state)
        self.env = patch.dict(os.environ, {"CLOUDFLARE_ACCOUNT_ID": ACCOUNT, "CLOUDFLARE_API_TOKEN": "BROAD-CANARY",
                                         "INGEST_CLOUDFLARE_API_TOKEN": "D1-CANARY", "TG_BOT_TOKEN": "BOT-CANARY"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.roots = patch.object(preview, "ROOT", self.root)
        self.roots.start()
        self.addCleanup(self.roots.stop)
        self.loader = patch.object(preview.tg_check, "load_env")
        self.loader.start()
        self.addCleanup(self.loader.stop)
        self.p = preview.Preview(self.state)
        self.source = sqlite3.connect(":memory:")
        self.target = sqlite3.connect(":memory:")
        self.source.row_factory = self.target.row_factory = sqlite3.Row
        self.addCleanup(self.source.close)
        self.addCleanup(self.target.close)
        for name in ["0001_init.sql", "0002_library_indexes.sql", "0003_artist_images.sql"]:
            self.source.executescript((ROOT / "worker/migrations" / name).read_text())
        self.source.executescript("CREATE TABLE d1_migrations(id INTEGER PRIMARY KEY,name TEXT,applied_at TEXT);"
                                  "INSERT INTO d1_migrations VALUES(1,'0001_init.sql','synthetic'),(2,'0002_library_indexes.sql','synthetic'),(3,'0003_artist_images.sql','synthetic');"
                                  "INSERT INTO artists VALUES('a','PRIVATE CANARY',NULL);"
                                  "INSERT INTO albums VALUES('b','a','PRIVATE CANARY',2026,'cover-canary');")
        self.source.execute("INSERT INTO tracks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            ("c", None, "b", "PRIVATE CANARY", 1, 1, 60, "mp3", "audio/mpeg", 3000, "file-canary", 42))
        self.source.commit()
        self.inventory = [{"uuid": SOURCE, "name": "cowlic"}]
        self.writes = []
        self.commands = []

    def api(self, suffix, data=None, d1=False):
        if suffix.startswith("d1/database?"):
            return self.inventory
        if suffix == f"d1/database/{SOURCE}":
            return {"file_size": 4096}
        if suffix == "workers/scripts":
            return []
        if suffix == "workers/scripts/cowlic/settings":
            return {"bindings": [{"type": "d1", "name": "DB", "id": SOURCE}]}
        if suffix == "workers/subdomain":
            return {"subdomain": "synthetic"}
        if suffix == "d1/database":
            self.assertEqual(data, {"name": preview.NAME})
            row = {"uuid": TARGET, "name": preview.NAME}
            self.inventory.append(row)
            return row
        target = suffix.split("/")[2]
        sql = data["sql"]
        if target == SOURCE:
            self.assertTrue(sql.startswith("SELECT name FROM sqlite_master WHERE name='cowlic_preview_probe_"))
        db = self.source if target == SOURCE else self.target
        if sql.startswith(("SELECT", "PRAGMA foreign_key_list")):
            rows = [dict(row) for row in db.execute(sql)]
        else:
            self.assertEqual(target, TARGET)
            self.writes.append(sql)
            db.executescript(sql)
            rows = []
        return [{"success": True, "results": rows}]

    def command(self, args, **kwargs):
        self.commands.append((args, kwargs))
        if args[:2] == ["d1", "export"]:
            db = self.source if args[2] == "cowlic" else self.target
            path = Path(args[args.index("--output") + 1])
            path.write_text("\n".join(db.iterdump()))
            path.chmod(0o600)
        elif args[:2] == ["d1", "execute"]:
            self.assertEqual(args[2], preview.NAME)
            self.target.executescript(Path(args[args.index("--file") + 1]).read_text())

    def initialize(self):
        with patch.object(self.p, "api", side_effect=self.api), patch.object(self.p, "command", side_effect=self.command), \
             patch.object(self.p, "check"), patch.object(preview.subprocess, "check_output", side_effect=[b"", "f" * 40]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.p.init()
        self.assertNotIn("CANARY", output.getvalue())

    def test_populated_snapshot_and_ledger_copy_refresh_and_isolation(self):
        before = list(self.source.iterdump())
        self.initialize()
        with patch.object(self.p, "api", side_effect=self.api), patch.object(self.p, "command", side_effect=self.command):
            self.target.execute("UPDATE tracks SET title='preview only'")
            self.target.commit()
            self.p.refresh()
        self.assertEqual(list(self.source.iterdump()), before)
        self.assertEqual(self.target.execute("SELECT name FROM d1_migrations").fetchone()[0], "0001_init.sql")
        self.assertEqual(self.target.execute("SELECT title FROM tracks").fetchone()[0], "PRIVATE CANARY")
        self.assertTrue(self.p.manifest["snapshot_verified"])
        self.assertFalse(self.p.manifest["ready"])
        self.assertTrue(any("cowlic_preview_probe_" in sql for sql in self.writes))

    def test_target_guard_rejects_each_identity_fault_before_write(self):
        self.initialize()
        baseline = dict(self.p.manifest)
        for key, value in [("account", "b" * 32), ("source", TARGET), ("worker", "cowlic"),
                           ("database_name", "cowlic"), ("database_id", SOURCE), ("database_id", "")]:
            with self.subTest(key=key, value=value), patch.object(self.p, "api", side_effect=self.api):
                self.p.manifest = {**baseline, key: value}
                count = len(self.writes)
                with self.assertRaises(preview.PreviewError):
                    self.p.query("DROP TABLE tracks")
                self.assertEqual(len(self.writes), count)
        self.p.manifest = baseline
        self.inventory[-1]["name"] = "unrelated"
        with patch.object(self.p, "api", side_effect=self.api), self.assertRaises(preview.PreviewError):
            self.p.write_config()

    def test_production_sql_is_read_only_and_allowlisted(self):
        for sql in ["DROP TABLE tracks", "SELECT 1; DROP TABLE tracks", "PRAGMA user_version=1", "SELECT * FROM tracks"]:
            with self.subTest(sql=sql), patch.object(self.p, "api") as api, self.assertRaises(preview.PreviewError):
                self.p.query(sql, source=True)
            api.assert_not_called()

    def test_existing_unowned_preview_name_is_not_adopted(self):
        self.inventory.append({"uuid": TARGET, "name": preview.NAME})
        with patch.object(self.p, "api", side_effect=self.api), self.assertRaises(preview.PreviewError):
            self.p.init()
        self.assertIsNone(self.p.manifest)

    def test_stale_local_source_refused_before_resource_creation(self):
        def api(suffix, *args, **kwargs):
            if suffix == "workers/scripts/cowlic/settings":
                return {"bindings": [{"type": "d1", "name": "DB", "id": TARGET}]}
            return self.api(suffix, *args, **kwargs)
        with patch.object(self.p, "api", side_effect=api), patch.object(self.p, "command") as command, \
             self.assertRaises(preview.PreviewError):
            self.p.init()
        self.assertEqual(len(self.inventory), 1)
        command.assert_not_called()

    def test_tampered_config_rejected_before_command(self):
        self.initialize()
        path = self.root / "worker/wrangler.preview.toml"
        original = path.read_text()
        for old, new in [('name = "cowlic-preview"', 'name = "cowlic"'),
                         ('invocation_logs = false', 'invocation_logs = true'),
                         ('redact_query_string = true', 'redact_query_string = false'),
                         ('binding = "DB"', 'binding = "OTHER"')]:
            path.write_text(original.replace(old, new))
            with self.subTest(old=old), patch.object(self.p, "api", side_effect=self.api), \
                 patch.object(self.p, "command") as command, self.assertRaises(preview.PreviewError):
                self.p.deploy()
            command.assert_not_called()

    def test_failed_import_marks_preview_unready_and_keeps_source(self):
        self.initialize()
        before = list(self.source.iterdump())
        def fail(args, **kwargs):
            if args[:2] == ["d1", "execute"]:
                raise preview.PreviewError("synthetic failure")
            return self.command(args, **kwargs)
        with patch.object(self.p, "api", side_effect=self.api), patch.object(self.p, "command", side_effect=fail), \
             self.assertRaises(preview.PreviewError):
            self.p.refresh()
        saved = preview.load(self.p.manifest_path)
        self.assertFalse(saved["ready"])
        self.assertFalse(saved["snapshot_verified"])
        self.assertEqual(list(self.source.iterdump()), before)
        self.assertFalse(list(self.state.glob("tmp*")))

    def test_private_files_and_source_secrets_not_reused_for_auth(self):
        self.initialize()
        for name in ["manifest.json", "access.json", "wrangler.preview.toml", "snapshot.sql"]:
            self.assertEqual(stat.S_IMODE((self.state / name).stat().st_mode), 0o600)
        access = preview.load(self.state / "access.json")
        self.assertEqual(access["username"], "preview")
        self.assertGreaterEqual(len(access["password"]), 32)
        bulk = next(kwargs for args, kwargs in self.commands if args[:2] == ["secret", "bulk"])
        self.assertEqual(set(json.loads(bulk["data"])), {"TG_BOT_TOKEN", "SUBSONIC_USER", "SUBSONIC_PASSWORD"})
        self.assertEqual(json.loads(bulk["data"])["TG_BOT_TOKEN"], "BOT-CANARY")
        self.assertNotIn("BROAD-CANARY", bulk["data"].decode())

    def test_private_state_rejects_symlinks(self):
        link = self.root / "alias"
        link.symlink_to(self.state)
        with self.assertRaises(preview.PreviewError):
            preview.private_dir(link)
        link.unlink()
        link.symlink_to(self.state / "missing")
        with self.assertRaises(preview.PreviewError):
            preview.load(link)

    def test_missing_migration_history_and_database_attachment_rejected(self):
        path = self.state / "bad.sql"
        path.write_text("CREATE TABLE artists(id TEXT);")
        with self.assertRaises(preview.PreviewError):
            preview.fingerprint(path)
        path.write_text("ATTACH DATABASE ':memory:' AS other;")
        with self.assertRaises(sqlite3.DatabaseError):
            preview.fingerprint(path)

    def test_wrong_account_endpoint_rejected_without_http(self):
        self.initialize()
        access = preview.load(self.state / "access.json")
        access["url"] = "https://cowlic-preview.someone-else.workers.dev"
        preview.save(self.state / "access.json", access)
        with patch.object(self.p, "api", side_effect=self.api), patch.object(preview.urllib.request, "build_opener") as opener, \
             self.assertRaises(preview.PreviewError):
            self.p.check()
        opener.assert_not_called()

    def test_authenticated_http_checks_ranges_and_no_redirects(self):
        self.initialize()
        ranges = []
        def open_response(request, timeout):
            url = preview.urllib.parse.urlsplit(request.full_url)
            params = preview.urllib.parse.parse_qs(url.query)
            method = url.path.removeprefix("/rest/")
            headers = Message()
            headers["Content-Type"] = "application/json"
            status = 200
            if "u" not in params:
                data = {"status": "failed", "error": {"code": 40}}
            else:
                self.assertIn("t", params)
                self.assertNotIn("BOT-CANARY", request.full_url)
                data = {"status": "ok", "song": {"id": "c"}}
            body = json.dumps({"subsonic-response": data}).encode()
            if method == "stream":
                byte_range = request.get_header("Range")
                ranges.append(byte_range)
                status = 206
                headers["Content-Range"] = byte_range.replace("=", " ") + "/3000"
                body = b"a" * 128
            if method == "getCoverArt":
                headers.replace_header("Content-Type", "image/jpeg")
                body = b"synthetic cover"
            response = io.BytesIO(body)
            response.status, response.headers = status, headers
            return response
        opener = unittest.mock.Mock()
        opener.open.side_effect = open_response
        with patch.object(self.p, "api", side_effect=self.api), \
             patch.object(preview.urllib.request, "build_opener", return_value=opener), contextlib.redirect_stdout(io.StringIO()):
            self.p.check()
        self.assertEqual(ranges, ["bytes=0-127", "bytes=1024-1151"])
        self.assertIsNone(preview.NoRedirect().redirect_request(None, None, 302, "redirect", None, "https://elsewhere.invalid"))

    def test_command_timeout_is_bounded_and_diagnostics_private(self):
        binary = self.root / "bin"
        binary.mkdir()
        fake = binary / "pnpm"
        fake.write_text('#!/bin/sh\nprintf "%s" "$CLOUDFLARE_API_TOKEN"\nsleep 5\n')
        fake.chmod(0o700)
        started = time.monotonic()
        with patch.dict(os.environ, {"PATH": str(binary) + ":/usr/bin:/bin"}), \
             contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(subprocess.TimeoutExpired):
            self.p.command(["synthetic"], maximum=0.05)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(output.getvalue(), "")
        log = self.state / "last-command.log"
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.assertEqual(log.read_text(), "D1-CANARY")

    def test_main_failure_marks_unready_without_exposing_error_payload(self):
        self.initialize()
        with tempfile.TemporaryDirectory() as outside:
            directory = Path(outside) / "preview"
            preview.private_dir(directory)
            self.p.manifest_path = directory / "manifest.json"
            preview.save(self.p.manifest_path, self.p.manifest)
            with patch.dict(os.environ, {"COWLIC_STATE_DIR": outside}), \
                 patch.object(preview.sys, "argv", ["preview-worker.py", "check"]), \
                 patch.object(preview, "Preview", return_value=self.p), \
                 patch.object(self.p, "check", side_effect=preview.urllib.error.URLError("BOT-CANARY PRIVATE-CANARY")), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(preview.main(), 1)
            self.assertNotIn("CANARY", output.getvalue())
            self.assertFalse(preview.load(self.p.manifest_path)["ready"])


if __name__ == "__main__":
    unittest.main()
