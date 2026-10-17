#!/usr/bin/env python3
"""Make a PRIVATE local review table for #353; never approve or apply a cover plan.

Run from the checkout: ingest/.venv/bin/python ingest/cover_plan_review.py
Only the existing parameterless D1 read is used. No YouTube or Telegram calls.
"""
import html
import os
from pathlib import Path
import tempfile

import bootstrap
import bootstrap_covers as covers

REVIEW_FILE = "cover-plan-review.html"


def render(plan, todo):
    stored = {t.album_id: t for t in todo}
    esc = html.escape
    rows = []
    for key, entry in sorted(plan.entries.items()):
        t = stored.get(key)
        artist, album = (t.artist, t.album) if t else ("Not in current cover-less set", "Recheck before apply")
        source = (f'<a href="https://music.youtube.com/browse/{esc(entry["browse_id"], quote=True)}" '
                  'target="_blank" rel="noopener noreferrer">Candidate album page</a>'
                  if entry['browse_id'] else 'Track-level match: candidate name was not retained')
        rows.append(f'<tr><td>{esc(artist)}</td><td>{esc(album)}</td><td>{esc(entry["rule"])}</td>'
                    f'<td>{source}</td><td><a href="{esc(entry["cover_url"], quote=True)}" '
                    f'target="_blank" rel="noopener noreferrer">Planned image</a></td><td>{esc(key)}</td></tr>')
    return ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">'
            '<title>Private cover plan review</title><style>body{font:16px sans-serif}td,th{padding:8px;border:1px solid #aaa}</style>'
            '<h1>Private cover plan review</h1><p>Contains private library names. Do not upload or paste this file.</p>'
            '<p>This table neither approves nor applies anything. Candidate names are absent from the plan; inspect '
            'the linked album page/image. Names shown are the current stored album and artist, not inferred candidates.</p>'
            f'<p>Plan approved: {str(plan.approved).lower()}. Entries: {len(rows)}.</p>'
            '<table><tr><th>Stored artist</th><th>Stored album</th><th>Rule</th><th>Candidate</th>'
            '<th>Cover</th><th>Album ID</th></tr>' + ''.join(rows) + '</table></html>')


def write_review(path: Path, text: str):
    """Atomic replacement, including a pre-existing symlink, always private."""
    fd, tmp = tempfile.mkstemp(prefix='.cover-review-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def main():
    directory = bootstrap.state_dir()
    try:
        lock = bootstrap.take_lock(directory)
    except bootstrap.Locked:
        print('cover review: another ingest session holds the lock')
        return 75
    except OSError:
        print('cover review: cannot take the lock')
        return 1
    try:
        plan = covers.load_plan(directory / covers.PLAN_FILE, bootstrap.today())
        if bootstrap.cover_env(bootstrap.D1_ENV):
            raise ValueError('configuration unavailable')
        todo, _ = covers.read_todo(bootstrap.cover_query())
        write_review(directory / REVIEW_FILE, render(plan, todo))
        print(f'cover review: wrote private {REVIEW_FILE} ({len(plan.entries)} entries); plan unchanged')
        return 0
    except Exception as e:
        # Exception text can contain credentials, SQL responses or library names.
        print(f'cover review: failed ({type(e).__name__}); no plan change')
        return 1
    finally:
        os.close(lock)


if __name__ == '__main__':
    raise SystemExit(main())
