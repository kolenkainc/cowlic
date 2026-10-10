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
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

API = "https://api.telegram.org"


def load_env() -> None:
    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


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
        sys.exit(f"Telegram API error {e.code}: {body.get('description', 'unknown')}")


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


def multipart(fields: dict, file_field: str, path: Path) -> urllib.request.Request:
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
                  f'filename="{path.name}"\r\nContent-Type: audio/mpeg\r\n\r\n').encode())
    parts += [path.read_bytes(), f"\r\n--{boundary}--\r\n".encode()]
    return boundary, parts


def roundtrip(token: str, chat_id: str, path: Path) -> None:
    boundary, parts = multipart({"chat_id": chat_id}, "audio", path)
    req = urllib.request.Request(f"{API}/bot{token}/sendAudio", data=b"".join(parts),
                                 headers={"content-type": f"multipart/form-data; boundary={boundary}"})
    msg = _send(req)["result"]
    audio = msg["audio"]
    print(f"uploaded: message_id={msg['message_id']} size={audio.get('file_size')} bytes")

    info = call(token, "getFile", file_id=audio["file_id"])["result"]
    with urllib.request.urlopen(f"{API}/file/bot{token}/{info['file_path']}", timeout=120) as r:
        downloaded = r.read()
    same = hashlib.sha256(downloaded).digest() == hashlib.sha256(path.read_bytes()).digest()
    print(f"downloaded {len(downloaded)} bytes, identical to original: {same}")
    sys.exit(0 if same else 1)


if __name__ == "__main__":
    load_env()
    token = os.environ.get("TG_BOT_TOKEN") or sys.exit("TG_BOT_TOKEN is not set")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "channel-id":
        channel_id(token)
    elif cmd == "roundtrip" and len(sys.argv) == 3:
        roundtrip(token, os.environ.get("TG_CHANNEL_ID") or sys.exit("TG_CHANNEL_ID is not set"), Path(sys.argv[2]))
    else:
        sys.exit(__doc__)
