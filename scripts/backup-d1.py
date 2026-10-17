#!/usr/bin/env python3
"""Private weekly D1 export. Production read only; raw Wrangler output never reaches the log."""
import argparse
import fcntl
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ingest"))
import tg_check

BACKUP_NAME = re.compile(r"cowlic-[0-9]{8}T[0-9]{6}Z-[a-z0-9_]{8}\.sql")


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("unsafe backup directory")
    path.chmod(0o700)


def backup(state_dir, keep):
    directory = state_dir / "backups"
    private_directory(state_dir)
    private_directory(directory)
    fd = os.open(directory / "backup.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        os.fchmod(lock.fileno(), 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("backup-d1: locked")
            return 75
        try:
            tg_check.load_env()
        except (tg_check.EnvSyntaxError, OSError):
            print("backup-d1: invalid local configuration")
            return 78
        token = os.environ.get("INGEST_CLOUDFLARE_API_TOKEN", "")
        account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
        if not token or not account or not (ROOT / "worker/wrangler.local.toml").is_file():
            print("backup-d1: missing token, account or local Wrangler config")
            return 78
        # Only the D1 token goes to Wrangler, never the broader personal token or unrelated service credentials.
        env = {k: os.environ[k] for k in ("PATH", "HOME", "XDG_CONFIG_HOME") if k in os.environ}
        env.update(CLOUDFLARE_API_TOKEN=token, CLOUDFLARE_ACCOUNT_ID=account,
                   WRANGLER_SEND_METRICS="false", CI="true")
        handle, name = tempfile.mkstemp(prefix=".pending-", suffix=".sql", dir=directory)
        os.close(handle)
        pending = Path(name)
        try:
            process = subprocess.Popen(
                ["npx", "--no-install", "wrangler", "d1", "export", "cowlic", "--remote", "--skip-confirmation", "--output", str(pending),
                 "-c", "wrangler.local.toml"], cwd=ROOT / "worker", env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                code = process.wait(timeout=600)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise
            if code != 0 or not pending.is_file() or pending.stat().st_size == 0:
                print("backup-d1: export failed")
                return 1
            pending.chmod(0o600)
            # Flush before publishing. An interrupted/failed export never replaces a completed backup.
            with pending.open("rb") as file:
                os.fsync(file.fileno())
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            suffix = pending.name.removeprefix(".pending-").removesuffix(".sql")
            complete = directory / f"cowlic-{stamp}-{suffix}.sql"
            pending.rename(complete)
            backups = sorted((p for p in directory.iterdir() if BACKUP_NAME.fullmatch(p.name)
                              and stat.S_ISREG(p.lstat().st_mode)),
                             key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
            for old in backups[keep:]:
                old.unlink()
            print(f"backup-d1: ok bytes={complete.stat().st_size} retained={min(keep, len(backups))}")
            return 0
        except (OSError, subprocess.TimeoutExpired):
            print("backup-d1: export or retention failed")
            return 1
        finally:
            pending.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", type=int, default=8, choices=range(1, 53), metavar="1..52")
    args = parser.parse_args()
    os.umask(0o077)
    state = Path(os.environ.get("COWLIC_STATE_DIR") or
                 str(Path(os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")) / "cowlic"))
    try:
        return backup(state, args.keep)
    except (OSError, ValueError):
        print("backup-d1: private state directory unavailable")
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
