"""Backup wrapper tests with an isolated checkout and a fake npx. No production requests."""
import fcntl
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "FAKE-NARROW-CANARY"
BROAD = "FAKE-BROAD-CANARY"


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("scripts", "worker", "ingest/.venv/bin", "bin"):
            (self.root / name).mkdir(parents=True)
        for name in ("scripts/backup-d1.py", "scripts/run-d1-backup.sh", "ingest/tg_check.py"):
            shutil.copy2(ROOT / name, self.root / name)
        (self.root / "ingest/.venv/bin/python").symlink_to(sys.executable)
        (self.root / "worker/wrangler.local.toml").write_text("# synthetic config only\n")
        # The project loader reads this synthetic file, never the real checkout's .env.
        (self.root / ".env").write_text(f"INGEST_CLOUDFLARE_API_TOKEN={TOKEN}\nCLOUDFLARE_API_TOKEN={BROAD}\n"
                                        "CLOUDFLARE_ACCOUNT_ID=FAKE-ACCOUNT\nTG_BOT_TOKEN=FAKE-BOT\n")
        stub = self.root / "bin/npx"
        stub.write_text(f'''#!{sys.executable}
import os,sys
from pathlib import Path
assert os.environ['CLOUDFLARE_API_TOKEN'] == {TOKEN!r}
assert 'INGEST_CLOUDFLARE_API_TOKEN' not in os.environ
assert 'TG_BOT_TOKEN' not in os.environ
assert sys.argv[1:7] == ['--no-install','wrangler','d1','export','cowlic','--remote']
assert sys.argv[-2:] == ['-c','wrangler.local.toml']
p=Path(sys.argv[sys.argv.index('--output')+1])
assert p.stat().st_mode & 0o777 == 0o600
mode=Path('../mode').read_text() if Path('../mode').exists() else 'ok'
print({BROAD!r})
print({TOKEN!r},file=sys.stderr)
p.write_text('CREATE TABLE synthetic (n INTEGER); INSERT INTO synthetic VALUES (42);\\n' if mode != 'empty' else '')
raise SystemExit(1 if mode == 'fail' else 0)
''')
        stub.chmod(0o700)
        self.env = {"PATH": f"{self.root / 'bin'}:/usr/bin:/bin", "HOME": str(self.root),
                    "COWLIC_STATE_DIR": str(self.root / "state")}
        self.backups = self.root / "state/backups"

    def run_backup(self, *args):
        return subprocess.run(["bash", str(self.root / "scripts/run-d1-backup.sh"), *args], env=self.env,
                              capture_output=True, text=True, timeout=10)

    def test_private_export_uses_only_narrow_token_and_counts_log(self):
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"^backup-d1: ok bytes=\d+ retained=1\n$")
        self.assertEqual(result.stderr, "")
        for canary in (TOKEN, BROAD, "FAKE-ACCOUNT", "CREATE TABLE", "synthetic"):
            self.assertNotIn(canary, result.stdout + result.stderr)
        (backup,) = list(self.backups.glob("cowlic-*.sql"))
        self.assertIn("VALUES (42)", backup.read_text())
        for path, mode in ((self.root / "state", 0o700), (self.backups, 0o700),
                           (backup, 0o600), (self.backups / "backup.lock", 0o600)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)

    def test_failed_or_empty_export_preserves_completed_backups_and_removes_partial(self):
        self.assertEqual(self.run_backup().returncode, 0)
        (original,) = list(self.backups.glob("cowlic-*.sql"))
        for mode in ("fail", "empty"):
            (self.root / "mode").write_text(mode)
            result = self.run_backup("--keep", "1")
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "backup-d1: export failed\n")
            self.assertEqual(list(self.backups.glob("cowlic-*.sql")), [original])
            self.assertEqual(list(self.backups.glob(".pending-*")), [])

    def test_retention_never_removes_unrelated_files_or_follows_symlinks(self):
        self.assertEqual(self.run_backup().returncode, 0)
        unrelated = self.backups / "notes.sql"
        unrelated.write_text("keep")
        target = self.root / "outside.sql"
        target.write_text("outside")
        link = self.backups / "cowlic-20000101T000000Z-abcdefgh.sql"
        link.symlink_to(target)
        for _ in range(3):
            self.assertEqual(self.run_backup("--keep", "2").returncode, 0)
        regular = [p for p in self.backups.glob("cowlic-*.sql") if not p.is_symlink()]
        self.assertEqual(len(regular), 2)
        self.assertEqual(unrelated.read_text(), "keep")
        self.assertEqual(target.read_text(), "outside")
        self.assertTrue(link.is_symlink())

    def test_lock_excludes_overlapping_exports(self):
        self.assertEqual(self.run_backup().returncode, 0)
        with (self.backups / "backup.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_backup()
        self.assertEqual((result.returncode, result.stdout), (75, "backup-d1: locked\n"))
        self.assertEqual(len(list(self.backups.glob("cowlic-*.sql"))), 1)

    def test_symlink_directory_is_rejected(self):
        target = self.root / "outside"
        target.mkdir()
        (self.root / "state").mkdir()
        self.backups.symlink_to(target, target_is_directory=True)
        result = self.run_backup()
        self.assertEqual(result.returncode, 78)
        self.assertEqual(list(target.iterdir()), [])

    def test_invalid_configuration_and_keep_fail_before_export(self):
        (self.root / ".env").write_text("BROKEN CONFIG\n")
        result = self.run_backup()
        self.assertEqual(result.returncode, 78)
        self.assertEqual(result.stdout, "backup-d1: invalid local configuration\n")
        for count in ("0", "53", "bad"):
            self.assertNotEqual(self.run_backup("--keep", count).returncode, 0)
        self.assertEqual(list(self.backups.glob("cowlic-*.sql")), [])


if __name__ == "__main__":
    unittest.main()
