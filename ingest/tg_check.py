"""Step 1 of the POC plan: verify Telegram as blob storage.

Usage:
  python ingest/tg_check.py channel-id          # prints channel ids seen in recent bot updates
  python ingest/tg_check.py roundtrip FILE.mp3  # upload, getFile, download, compare sha256

Reads TG_BOT_TOKEN (and TG_CHANNEL_ID for roundtrip) from the environment or ./.env.
The token is never printed.
"""
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

API = "https://api.telegram.org"


class TelegramError(Exception):
    """Telegram API failure. The message never contains the request URL (it holds the bot token)."""


class EnvSyntaxError(Exception):
    """Unsupported .env syntax. The message holds line numbers and reasons, never a value."""


_ENV_LINE = re.compile(r"[ \t]*([A-Za-z_][A-Za-z0-9_]*)=(.*)")
_ENV_SKIP = re.compile(rb"[ \t]*(#.*)?")


def parse_env(data: bytes) -> dict[str, str]:
    """Parse ./.env from its bytes. Supported: blank lines, comment lines (first non-blank char is #, any bytes
    after it), and KEY=value (KEY is [A-Za-z_][A-Za-z0-9_]*, no space around =). The value is taken verbatim, trailing
    spaces/tabs are trimmed and matching quotes around the whole value are removed; an empty value counts as missing
    for the caller. Rejected, naming only the line (never the value): `export`, a value starting with a space, an
    unbalanced quote or text after the closing quote, an unquoted ' #' inline comment, a repeated key, a NUL byte, a
    CR that is not directly before a line feed, a non-ASCII byte outside a comment, any other line."""
    if b"\x00" in data:
        raise EnvSyntaxError("unsupported .env syntax: the file contains a NUL byte")
    out: dict[str, str] = {}
    problems = []
    parts = data.split(b"\n")
    for n, raw in enumerate(parts, 1):
        if raw.endswith(b"\r") and n < len(parts):  # CRLF; a CR at the very end of the file is a lone CR
            raw = raw[:-1]
        if b"\r" in raw:
            problems.append(f"line {n}: carriage return not directly before a line feed")
            continue
        if _ENV_SKIP.fullmatch(raw):
            continue
        if not raw.isascii():
            problems.append(f"line {n}: non-ASCII byte outside a comment")
            continue
        line = raw.decode("ascii")
        m = _ENV_LINE.fullmatch(line)
        if not m:
            reason = ("'export' is not supported, write KEY=value" if re.match(r"[ \t]*export[ \t]", line)
                      else "not a KEY=value line (no spaces around '=')")
            problems.append(f"line {n}: {reason}")
            continue
        key, val = m.group(1), m.group(2).rstrip(" \t")
        if val[:1] in (" ", "\t"):
            problems.append(f"line {n}: value starts with whitespace (key {key})")
        elif len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
            val = val[1:-1]
        elif val[:1] in ("\"", "'"):
            problems.append(f"line {n}: unbalanced quote or text after the closing quote (key {key})")
        elif re.search(r"[ \t]#", val):
            problems.append(f"line {n}: unquoted inline comment (' #') (key {key})")
        if key in out:
            problems.append(f"line {n}: key {key} is defined twice")
        out[key] = val
    if problems:
        raise EnvSyntaxError("unsupported .env syntax: " + "; ".join(problems))
    return out


def load_env() -> None:
    """Load ./.env into os.environ without overriding variables that are already set (an explicit environment
    wins). Raises EnvSyntaxError on unsupported syntax."""
    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        return
    for k, v in parse_env(env.read_bytes()).items():
        os.environ.setdefault(k, v)


def call(token: str, method: str, **params) -> dict:
    req = urllib.request.Request(f"{API}/bot{token}/{method}", data=json.dumps(params).encode(),
                                 headers={"content-type": "application/json"})
    return _send(req)


def _send(req: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        # Report Telegram's error description only; never echo the request URL (it contains the token).
        body = json.load(e) if e.headers.get_content_type() == "application/json" else {}
        raise TelegramError(f"Telegram API error {e.code}: {body.get('description', 'unknown')}") from None


def channel_id(token: str) -> None:
    seen = {}
    for u in call(token, "getUpdates", allowed_updates=["channel_post"])["result"]:
        chat = u.get("channel_post", {}).get("chat")
        if chat:
            seen[chat["id"]] = chat.get("title", "")
    if not seen:
        sys.exit("No channel posts seen. Post any message in the channel (bot must be admin), then rerun.")
    for cid, title in seen.items():
        print(f"{cid}\t{title}")


def multipart(fields: dict, file_field: str, path: Path, content_type: str = "audio/mpeg"):
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
                  f'filename="{path.name}"\r\nContent-Type: {content_type}\r\n\r\n').encode())
    parts += [path.read_bytes(), f"\r\n--{boundary}--\r\n".encode()]
    return boundary, parts


def roundtrip(token: str, chat_id: str, path: Path) -> None:
    boundary, parts = multipart({"chat_id": chat_id}, "audio", path)
    req = urllib.request.Request(f"{API}/bot{token}/sendAudio", data=b"".join(parts),
                                 headers={"content-type": f"multipart/form-data; boundary={boundary}"})
    msg = _send(req)["result"]
    audio = msg["audio"]
    print(f"uploaded: message_id={msg['message_id']} file_id={audio['file_id']} size={audio.get('file_size')} bytes")

    info = call(token, "getFile", file_id=audio["file_id"])["result"]
    with urllib.request.urlopen(f"{API}/file/bot{token}/{info['file_path']}", timeout=120) as r:
        downloaded = r.read()
    same = hashlib.sha256(downloaded).digest() == hashlib.sha256(path.read_bytes()).digest()
    print(f"downloaded {len(downloaded)} bytes, identical to original: {same}")
    sys.exit(0 if same else 1)


def main() -> None:
    load_env()
    token = os.environ.get("TG_BOT_TOKEN") or sys.exit("TG_BOT_TOKEN is not set")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "channel-id":
        channel_id(token)
    elif cmd == "roundtrip" and len(sys.argv) == 3:
        roundtrip(token, os.environ.get("TG_CHANNEL_ID") or sys.exit("TG_CHANNEL_ID is not set"), Path(sys.argv[2]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    try:
        main()
    except (TelegramError, EnvSyntaxError) as e:
        sys.exit(str(e))
