"""Tests for bootstrap_ranking (#167). Offline: a fake Last.fm client, a temp state directory, no network."""
import datetime
import json
import os
import random
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import bootstrap_ranking as br
import ingest
import lastfm
from test_lastfm_source import NoNetwork

NOW = datetime.datetime(2026, 10, 11, 18, 0, 0, tzinfo=datetime.timezone.utc)
MBID = "5b11f4ce-a62d-471e-81fc-a69a8278c7da"


def art(name, playcount, mbid=None):
    return {"name": name, "mbid": mbid, "playcount": playcount}


class FakeClient:
    """Serves one weekly chart list with one week and one artist chart; counts calls."""

    def __init__(self, artists, fail=False):
        self.artists, self.fail, self.calls = artists, fail, 0

    def call(self, method, **params):
        self.calls += 1
        if self.fail:
            raise lastfm.LastfmError("boom")
        if method == "user.getWeeklyChartList":
            f = int((NOW - datetime.timedelta(days=30)).timestamp())
            return {"weeklychartlist": {"chart": [{"from": str(f), "to": str(f + 604800)}]}}
        return {"weeklyartistchart": {"artist": [{"name": a["name"], "mbid": a["mbid"] or "",
                                                  "playcount": str(a["playcount"])} for a in self.artists]}}


class Base(NoNetwork):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "state" / "cowlic"
        self.path = self.dir / br.FILE_NAME

    def doc(self, artists=None, **over):
        d = {"version": 1, "computed_at": "2026-10-11T18:00:00Z", "years": 5, "max_artists": 100,
             "artists": artists if artists is not None else [{"name": "A", "playcount": 5}, {"name": "B", "playcount": 3}]}
        d.update(over)
        return d

    def write(self, doc):
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(doc if isinstance(doc, str) else json.dumps(doc))

    def refuses(self, doc, kind="invalid"):
        self.write(doc)
        with self.assertRaises(br.RankingError) as cm:
            br.load(self.path)
        self.assertEqual(cm.exception.kind, kind)
        return str(cm.exception)


class OrderTest(Base):
    def test_playcount_descending_then_normalised_name(self):
        got = br.order([art("b", 5), art("A", 5), art("z", 9), art("c", 1)])
        self.assertEqual([a["name"] for a in got], ["z", "A", "b", "c"])

    def test_tie_uses_match_normalize_not_casefold(self):
        # normalize drops punctuation: "!!! x" sorts as "x", after "b"; a plain casefold would put "!" first
        got = br.order([art("!!! x", 5), art("b", 5)])
        self.assertEqual([a["name"] for a in got], ["b", "!!! x"])

    def test_shuffled_input_gives_one_order(self):
        items = [art(f"Band {i % 7}", 10 - i % 3) for i in range(30)] + [art("Band 1", 8), art("band 1", 8)]
        want = br.order(items)
        for seed in range(20):
            shuffled = items[:]
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(br.order(shuffled), want)

    def test_does_not_mutate_input(self):
        items = [art("b", 1), art("a", 2)]
        br.order(items)
        self.assertEqual([a["name"] for a in items], ["b", "a"])


class BuildTest(Base):
    def test_entries_have_name_key_playcount_rank(self):
        r = br.build([art("Beta", 5), art("Alpha", 9)], 5, 100, NOW)
        self.assertEqual([a["name"] for a in r["artists"]], ["Alpha", "Beta"])
        self.assertEqual([a["rank"] for a in r["artists"]], [1, 2])
        self.assertEqual(r["artists"][0]["key"], ingest.lastfm_artist_key("Alpha"))
        self.assertEqual(r["computed_at"], "2026-10-11T18:00:00Z")
        self.assertEqual((r["years"], r["max_artists"], r["version"]), (5, 100, 1))

    def test_mbid_only_when_valid_and_never_empty(self):
        r = br.build([art("A", 3, MBID.upper()), art("B", 2, ""), art("C", 1, "not-an-id"), art("D", 0, None)], 5, 100, NOW)
        by = {a["name"]: a for a in r["artists"]}
        self.assertEqual(by["A"]["mbid"], MBID)
        for n in "BCD":
            self.assertNotIn("mbid", by[n])

    def test_cut_to_max_artists_and_duplicate_keys_dropped(self):
        r = br.build([art("Same", 9, MBID), art("same", 4), art("X", 3), art("Y", 2)], 5, 2, NOW)
        self.assertEqual([a["name"] for a in r["artists"]], ["Same", "X"])
        self.assertEqual(r["artists"][0]["playcount"], 9)

    def test_rejects_bad_input(self):
        for args in (([], 5, 100), ([art("A", 1)], 0, 100), ([art("A", 1)], 5, 0), ([art("A", 1)], True, 100),
                     ([art("A", 1)], lastfm.MAX_YEARS + 1, 100)):
            with self.assertRaises(br.RankingError):
                br.build(*args)
        for bad in (art("", 1), art("A", -1), art("A", True), art("A", "5"), {"playcount": 1}):
            with self.assertRaises(br.RankingError):
                br.build([bad], 5, 100)
        with self.assertRaises(br.RankingError):
            br.build([art("A", 1)], 5, 100, datetime.datetime(2026, 1, 1))

    def test_age_days(self):
        r = br.build([art("A", 1)], 5, 100, NOW)
        self.assertEqual(br.age_days(r, NOW + datetime.timedelta(days=40, hours=3)), 40)
        self.assertEqual(br.age_days(r, NOW - datetime.timedelta(days=2)), 0)


class SaveLoadTest(Base):
    def ranking(self):
        return br.build([art("Alpha", 9, MBID), art("Beta", 5), art("Cé", 5)], 5, 100, NOW)

    def test_round_trip_and_stored_shape(self):
        r = self.ranking()
        br.save(self.path, r)
        self.assertEqual(br.load(self.path), r)
        stored = json.loads(self.path.read_text())
        self.assertEqual(set(stored), {"version", "computed_at", "years", "max_artists", "artists"})
        self.assertEqual(stored["artists"][0], {"name": "Alpha", "playcount": 9, "mbid": MBID})
        self.assertEqual(stored["artists"][1], {"name": "Beta", "playcount": 5})

    def test_modes_and_private_dir(self):
        br.save(self.path, self.ranking())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        self.assertEqual(os.listdir(self.dir), [br.FILE_NAME])  # no temp file left

    def test_mode_stays_0600_when_overwriting_a_loose_file(self):
        self.write(self.doc())
        os.chmod(self.path, 0o644)
        br.save(self.path, self.ranking())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_failed_write_keeps_old_file_and_leaves_no_temp(self):
        br.save(self.path, self.ranking())
        before = self.path.read_bytes()
        newer = br.build([art("Zed", 1)], 5, 100, NOW)
        with mock.patch("os.replace", side_effect=OSError("disk")):
            with self.assertRaises(br.RankingError) as cm:
                br.save(self.path, newer)
        self.assertEqual(cm.exception.kind, "unwritable")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(os.listdir(self.dir), [br.FILE_NAME])

    def test_write_goes_through_rename_of_a_sibling_temp(self):
        calls = []
        real = os.replace
        with mock.patch("os.replace", side_effect=lambda a, b: (calls.append((Path(a), Path(b))), real(a, b))[1]):
            br.save(self.path, self.ranking())
        (src, dst), = calls
        self.assertEqual(dst, self.path)
        self.assertEqual(src.parent, self.path.parent)
        self.assertNotEqual(src, dst)

    def test_reader_during_replace_sees_old_or_new_never_partial(self):
        one = br.build([art(f"A{i}", 1000 - i) for i in range(200)], 5, 200, NOW)
        two = br.build([art(f"B{i}", 1000 - i) for i in range(200)], 5, 200, NOW)
        br.save(self.path, one)
        stop, bad, seen = threading.Event(), [], set()

        def reader():
            while not stop.is_set():
                try:
                    seen.add(br.load(self.path)["artists"][0]["name"])
                except br.RankingError as e:
                    bad.append(str(e))
        t = threading.Thread(target=reader)
        t.start()
        try:
            for i in range(60):
                br.save(self.path, two if i % 2 == 0 else one)
        finally:
            stop.set()
            t.join()
        self.assertEqual(bad, [])
        self.assertLessEqual(seen, {"A0", "B0"})

    def test_save_refuses_what_load_would_refuse(self):
        r = self.ranking()
        for mutate in (lambda d: d["artists"].reverse(),
                       lambda d: d.update(version=2),
                       lambda d: d["artists"][0].update(name=""),
                       lambda d: d["artists"][0].update(playcount="9"),
                       lambda d: d.update(computed_at="yesterday"),
                       lambda d: d["artists"].append(dict(d["artists"][0]))):
            d = json.loads(json.dumps(r))
            mutate(d)
            with self.assertRaises(br.RankingError):
                br.save(self.path, d)
            self.assertFalse(self.path.exists())
        with self.assertRaises(br.RankingError):
            br.save(self.path, "nope")

    def test_save_does_not_store_key_or_rank(self):
        br.save(self.path, self.ranking())
        text = self.path.read_text()
        self.assertNotIn('"key"', text)
        self.assertNotIn('"rank"', text)


class LoadStrictTest(Base):
    def test_missing_file_is_kind_missing(self):
        with self.assertRaises(br.RankingError) as cm:
            br.load(self.path)
        self.assertEqual(cm.exception.kind, "missing")

    def test_read_reports_no_ranking_with_a_reason(self):
        self.assertEqual(br.read(self.path)[0], None)
        self.assertIn("no ranking", br.read(self.path)[1])
        self.write("{not json")
        got, reason = br.read(self.path)
        self.assertIsNone(got)
        self.assertTrue(reason)
        self.write(self.doc())
        got, reason = br.read(self.path)
        self.assertEqual((got["artists"][1]["rank"], reason), (2, ""))

    def test_corrupt_inputs_raise_never_an_empty_list(self):
        for raw in ("", "{", "[]", "null", "42", '{"version": 1}', "\xff", '{"version": NaN}',
                    "[" * 100000):
            self.refuses(raw)
        self.write(b"\xff\xfe".decode("latin-1"))  # not UTF-8 once written as UTF-8? still must not crash
        with self.assertRaises(br.RankingError):
            br.load(self.path)
        self.path.write_bytes(b"\xff\xfe\x00")
        with self.assertRaises(br.RankingError):
            br.load(self.path)

    def test_future_and_old_versions_are_refused(self):
        for v in (2, 99, 0):
            self.assertIn("version", self.refuses(self.doc(version=v), "version"))
        for v in ("1", None, True, 1.0 + 0.5):
            self.refuses(self.doc(version=v))

    def test_unknown_and_missing_top_level_fields(self):
        self.refuses(self.doc(extra=1))
        d = self.doc()
        del d["years"]
        self.refuses(d)
        d = self.doc()
        d["user"] = "someone"
        self.refuses(d)

    def test_field_checks(self):
        bad = [dict(computed_at="2026-10-11"), dict(computed_at="2026-13-45T00:00:00Z"), dict(computed_at=5),
               dict(computed_at="2026-10-11T18:00:00+00:00"), dict(years=0), dict(years=21), dict(years="5"),
               dict(years=True), dict(max_artists=0), dict(max_artists=1001), dict(max_artists=1),
               dict(artists=[]), dict(artists={}), dict(artists="x")]
        for over in bad:
            self.refuses(self.doc(**over))

    def test_entry_checks(self):
        good = {"name": "A", "playcount": 5}
        cases = [{}, {"name": "A"}, {"playcount": 5}, {**good, "extra": 1}, {**good, "url": "http://x"},
                 {"name": "", "playcount": 5}, {"name": 5, "playcount": 5}, {"name": " A", "playcount": 5},
                 {"name": "A\x00", "playcount": 5}, {"name": "A​B", "playcount": 5},
                 {"name": "A  B", "playcount": 5}, {"name": "x" * 500, "playcount": 5},
                 {**good, "playcount": -1}, {**good, "playcount": 1.5}, {**good, "playcount": "5"},
                 {**good, "playcount": True}, {**good, "playcount": 10 ** 12 + 1},
                 {**good, "mbid": ""}, {**good, "mbid": "nope"}, {**good, "mbid": MBID.upper()},
                 {**good, "mbid": None}, {**good, "mbid": 5}, "A", None, [], 5]
        for entry in cases:
            self.refuses(self.doc(artists=[entry]))

    def test_order_and_duplicates_are_enforced(self):
        self.refuses(self.doc(artists=[{"name": "A", "playcount": 3}, {"name": "B", "playcount": 5}]))
        self.refuses(self.doc(artists=[{"name": "B", "playcount": 5}, {"name": "A", "playcount": 5}]))
        self.refuses(self.doc(artists=[{"name": "A", "playcount": 5}, {"name": "a", "playcount": 4}]))
        self.refuses(self.doc(artists=[{"name": "A", "playcount": 5}, {"name": "B", "playcount": 4}], max_artists=1))

    def test_size_cap_and_non_regular_file(self):
        self.write(" " * (br.MAX_BYTES + 1))
        self.assertIn("larger", self.refuses(" " * (br.MAX_BYTES + 1)))
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(br.RankingError):
            br.load(self.path)

    def test_unreadable_file(self):
        self.write(self.doc())
        with mock.patch("os.open", side_effect=PermissionError("x")):
            with self.assertRaises(br.RankingError):
                br.load(self.path)

    def test_error_messages_hold_no_artist_names_or_content(self):
        secret = "Secret Artist Name"
        for doc in (self.doc(artists=[{"name": secret, "playcount": "x"}]),
                    self.doc(artists=[{"name": secret, "playcount": 1, "url": "u"}]),
                    self.doc(artists=[{"name": secret + "\x00", "playcount": 1}]),
                    self.doc(artists=[{"name": "a", "playcount": 1}, {"name": secret, "playcount": 2}]),
                    "{" + secret):
            self.assertNotIn(secret, self.refuses(doc))

    def test_loaded_data_is_a_copy_with_keys(self):
        self.write(self.doc())
        r = br.load(self.path)
        self.assertEqual(r["artists"][0]["key"], ingest.lastfm_artist_key("A"))


class DefaultPathTest(Base):
    def test_default_path_is_in_the_state_dir(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/x/state"}):
            self.assertEqual(br.default_path(), Path("/x/state/cowlic/bootstrap-ranking.json"))
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": ""}):
            self.assertEqual(br.default_path(), Path.home() / ".local/state/cowlic/bootstrap-ranking.json")

    def test_none_path_means_default(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.dir.parent)}):
            br.save(None, br.build([art("A", 1)], 5, 100, NOW))
            self.assertTrue(self.path.exists())
            self.assertEqual(br.load(None)["artists"][0]["name"], "A")


class ComputeTest(Base):
    def run_compute(self, client, **kw):
        quiet = []
        r = br.compute(client, "some_user", 5, kw.pop("max_artists", 100), self.path, now=NOW, out=quiet.append, **kw)
        return r, quiet

    def test_compute_stores_and_returns_the_ordered_ranking(self):
        client = FakeClient([art("Beta", 5), art("Alpha", 9, MBID), art("Aa", 5)])
        r, out = self.run_compute(client)
        self.assertEqual([a["name"] for a in r["artists"]], ["Alpha", "Aa", "Beta"])
        self.assertEqual(br.load(self.path), r)
        self.assertEqual(r["artists"][0]["mbid"], MBID)
        self.assertEqual(client.calls, 2)

    def test_second_compute_refused_without_refresh_and_makes_no_call(self):
        self.run_compute(FakeClient([art("A", 1)]))
        before = self.path.read_bytes()
        again = FakeClient([art("Z", 99)])
        with self.assertRaises(br.RankingError) as cm:
            self.run_compute(again)
        self.assertEqual(cm.exception.kind, "exists")
        self.assertEqual(again.calls, 0)
        self.assertEqual(self.path.read_bytes(), before)

    def test_refresh_replaces(self):
        self.run_compute(FakeClient([art("A", 1)]))
        self.run_compute(FakeClient([art("Z", 99)]), refresh=True)
        self.assertEqual(br.load(self.path)["artists"][0]["name"], "Z")

    def test_failed_compute_never_overwrites_a_good_file(self):
        self.run_compute(FakeClient([art("A", 1)]))
        before = self.path.read_bytes()
        with self.assertRaises(lastfm.LastfmError):
            self.run_compute(FakeClient([], fail=True), refresh=True)
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaises(lastfm.LastfmError):  # empty charts
            self.run_compute(FakeClient([]), refresh=True)
        self.assertEqual(self.path.read_bytes(), before)

    def test_failed_first_compute_creates_nothing(self):
        with self.assertRaises(lastfm.LastfmError):
            self.run_compute(FakeClient([], fail=True))
        self.assertFalse(self.path.exists())

    def test_write_failure_keeps_old_file(self):
        self.run_compute(FakeClient([art("A", 1)]))
        before = self.path.read_bytes()
        with mock.patch("os.replace", side_effect=OSError("disk")):
            with self.assertRaises(br.RankingError):
                self.run_compute(FakeClient([art("Z", 5)]), refresh=True)
        self.assertEqual(self.path.read_bytes(), before)

    def test_bad_parameters_are_refused_before_any_write(self):
        for years, mx in ((0, 100), (5, 0), (5, 5000)):
            with self.assertRaises(lastfm.LastfmError):
                br.compute(FakeClient([art("A", 1)]), "some_user", years, mx, self.path, now=NOW, out=lambda s: None)
        self.assertFalse(self.path.exists())

    def test_no_user_name_key_or_url_in_the_file_and_no_names_in_output(self):
        r, out = self.run_compute(FakeClient([art("Secret Band", 4)]))
        text = self.path.read_text()
        for forbidden in ("some_user", "api_key", "http", "LASTFM", "user"):
            self.assertNotIn(forbidden, text)
        self.assertNotIn("Secret Band", " ".join(out))
        self.assertTrue(any("1 artists" in line for line in out))


if __name__ == "__main__":
    unittest.main()
