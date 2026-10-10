"""Tests for lastfm: a fake Last.fm behind an injected fetch (plus a loopback server for the HTTP layer).
No real network, no sleeping, no real key."""
import contextlib
import datetime
import io
import os
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

import lastfm as lf

REAL_CLIENT = lf.LastfmClient  # main() is run with the class patched; the factories need the real one

KEY = "canaryKey0123456789abcdefABCDEF01"
USER = "fake_user"
WEEK = 7 * 86400
NOW = datetime.datetime(2026, 10, 10, 12, 0, tzinfo=datetime.timezone.utc)
MBID = "11111111-2222-3333-4444-555555555555"


def week_ending(now: datetime.datetime, back: int) -> tuple[int, int]:
    end = int(now.timestamp()) - back * WEEK
    return end - WEEK, end


def chart_list(ranges):
    return {"weeklychartlist": {"chart": [{"#text": "", "from": str(f), "to": str(t)} for f, t in ranges]}}


def artist_chart(entries):
    return {"weeklyartistchart": {"artist": [
        {"name": n, "mbid": m, "playcount": str(p), "url": "https://example.invalid/x"} for n, m, p in entries]}}


class Fake:
    """fetch(url, timeout) over in-memory answers. `answers` maps (from, to) -> chart body; scripted `errors` are
    consumed first. Records every url (they hold the key: tests only inspect them)."""

    def __init__(self, ranges, answers=None, errors=()):
        self.ranges, self.answers, self.errors, self.urls = ranges, answers or {}, list(errors), []

    def __call__(self, url, timeout):
        self.urls.append(url)
        if self.errors:
            e = self.errors.pop(0)
            if isinstance(e, Exception):
                raise e
            return e
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        assert q["api_key"] == KEY and q["format"] == "json"
        if q["method"] == "user.getWeeklyChartList":
            return 200, chart_list(self.ranges)
        rng = (int(q["from"]), int(q["to"]))
        return 200, self.answers.get(rng, artist_chart([]))


class Clock:
    def __init__(self):
        self.t, self.sleeps = 1000.0, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def make(fake, **kw):
    clk = Clock()
    kw.setdefault("out", lambda s: None)
    c = lf.LastfmClient(KEY, fetch=fake, sleep=clk.sleep, clock=clk.now, **kw)
    return c, clk


class WindowTests(unittest.TestCase):
    def test_window_boundaries(self):
        start = lf._years_ago(NOW, 5).timestamp()
        inside = (int(start), int(start) + WEEK)
        straddle = (int(start) - 1, int(start) + WEEK - 1)  # begins one second before the window
        late = (int(NOW.timestamp()) - WEEK, int(NOW.timestamp()))  # ends exactly now: inside
        future = (int(NOW.timestamp()) - WEEK + 1, int(NOW.timestamp()) + 1)  # ends after now
        old = (int(start) - WEEK, int(start))
        got = lf.select_weeks(sorted([old, straddle, inside, late, future]), NOW, 5)
        self.assertEqual(got, [inside, late])

    def test_leap_day_now(self):
        self.assertEqual(lf._years_ago(datetime.datetime(2028, 2, 29, tzinfo=datetime.timezone.utc), 1).day, 28)

    def test_top_artists_uses_only_weeks_in_window(self):
        old = week_ending(NOW, 5 * 53)
        new = week_ending(NOW, 1)
        fake = Fake([old, new], {old: artist_chart([("Old", None, 99)]), new: artist_chart([("New", None, 1)])})
        c, _ = make(fake)
        res = lf.top_artists(c, USER, 5, NOW)
        self.assertEqual([a["name"] for a in res], ["New"])
        self.assertEqual(c.calls, 2)  # chart list + one week; the old week is never fetched


class AggregationTests(unittest.TestCase):
    def run_top(self, answers, ranges=None, **kw):
        ranges = ranges or sorted(answers)
        c, _ = make(Fake(ranges, answers))
        return lf.top_artists(c, USER, 5, NOW, **kw)

    def test_sum_across_weeks_and_sort_with_ties(self):
        w1, w2 = week_ending(NOW, 2), week_ending(NOW, 1)
        res = self.run_top({
            w1: artist_chart([("Beta", None, 5), ("Alpha", None, 3), ("Gamma", None, 1)]),
            w2: artist_chart([("alpha", None, 2), ("Delta", None, 5)])})
        self.assertEqual([(a["name"], a["playcount"]) for a in res],
                         [("Alpha", 5), ("Beta", 5), ("Delta", 5), ("Gamma", 1)])  # first seen display name; tie by name

    def test_mbid_key_merges_different_spellings(self):
        w1, w2 = week_ending(NOW, 2), week_ending(NOW, 1)
        res = self.run_top({w1: artist_chart([("The Band", MBID, 4)]), w2: artist_chart([("Band, The", MBID.upper(), 6)])})
        self.assertEqual(res, [{"name": "The Band", "mbid": MBID, "playcount": 10}])

    def test_artist_without_mbid_and_bad_mbid(self):
        w1 = week_ending(NOW, 1)
        res = self.run_top({w1: artist_chart([("No Id", "", 2), ("Bad Id", "not-a-uuid", 1)])})
        self.assertEqual([(a["name"], a["mbid"]) for a in res], [("No Id", None), ("Bad Id", None)])

    def test_empty_weeks_ok_but_all_empty_is_error(self):
        w1, w2 = week_ending(NOW, 2), week_ending(NOW, 1)
        res = self.run_top({w2: artist_chart([("A", None, 1)])}, ranges=[w1, w2])
        self.assertEqual(len(res), 1)
        with self.assertRaisesRegex(lf.LastfmError, "empty or private"):
            self.run_top({}, ranges=[w1, w2])

    def test_no_weeks_in_window_is_error(self):
        with self.assertRaisesRegex(lf.LastfmError, "no weekly charts"):
            self.run_top({}, ranges=[week_ending(NOW, 5 * 53 + 10)])

    def test_max_artists_cuts(self):
        w1 = week_ending(NOW, 1)
        res = self.run_top({w1: artist_chart([(f"A{i}", None, 100 - i) for i in range(10)])}, max_artists=3)
        self.assertEqual([a["name"] for a in res], ["A0", "A1", "A2"])

    def test_single_artist_as_object_and_empty_week_without_key(self):
        w1, w2 = week_ending(NOW, 2), week_ending(NOW, 1)
        answers = {w1: {"weeklyartistchart": {"artist": {"name": "Solo", "mbid": "", "playcount": "7"}}},
                   w2: {"weeklyartistchart": {"@attr": {}}}}
        res = self.run_top(answers, ranges=[w1, w2])
        self.assertEqual([(a["name"], a["playcount"]) for a in res], [("Solo", 7)])

    def test_untrusted_fields(self):
        w1 = week_ending(NOW, 1)
        entries = [
            {"name": "Ev\x1b[31mil\x00‮  Name\n", "mbid": "", "playcount": "3"},
            {"name": "", "mbid": "", "playcount": "3"},  # no name: dropped
            {"name": "NegCount", "mbid": "", "playcount": "-1"},  # bad count: dropped
            {"name": "Float", "mbid": "", "playcount": "1.5"},
            {"name": 5, "mbid": "", "playcount": "1"},
            "junk", None,
            {"name": "Long" + "x" * 500, "mbid": "", "playcount": "1"}]
        res = self.run_top({w1: {"weeklyartistchart": {"artist": entries}}})
        self.assertEqual(res[0]["name"], "Ev[31mil Name")  # control and format characters removed, spaces collapsed
        self.assertEqual(len(res), 2)
        self.assertLessEqual(len(res[1]["name"]), lf.NAME_MAX + 3)

    def test_malformed_structures(self):
        w1 = week_ending(NOW, 1)
        for bad in ({}, {"weeklyartistchart": []}, {"weeklyartistchart": {"artist": "x"}}):
            with self.assertRaises(lf.LastfmError):
                lf.parse_artist_chart(bad)
        for bad in ({}, {"weeklychartlist": {"chart": [{"from": "x", "to": "2"}]}},
                    {"weeklychartlist": {"chart": [{"from": "5", "to": "5"}]}},
                    {"weeklychartlist": {"chart": [{"from": "1", "to": "10"}, {"from": "5", "to": "20"}]}}):
            with self.assertRaises(lf.LastfmError):
                lf.parse_chart_list(bad)
        self.assertEqual(lf.parse_chart_list(chart_list([w1, w1])), [w1])  # duplicate range counted once

    def test_corrupt_chart_list_stops_before_fetching_weeks(self):
        start = int(lf._years_ago(NOW, 1).timestamp())
        many = [(start + i * 10, start + i * 10 + 10) for i in range(1000)]
        fake = Fake(many)
        c, _ = make(fake)
        with self.assertRaisesRegex(lf.LastfmError, "corrupt"):
            lf.top_artists(c, USER, 1, NOW)
        self.assertEqual(c.calls, 1)

    def test_argument_validation(self):
        c, _ = make(Fake([]))
        for kw in ({"user": "a"}, {"user": "bad user!"}, {"user": USER, "years": 0}, {"user": USER, "years": True},
                   {"user": USER, "max_artists": 0}, {"user": USER, "max_artists": 10 ** 6}):
            kw.setdefault("user", USER)
            with self.assertRaises(lf.LastfmError):
                lf.top_artists(c, now=NOW, **kw)
        with self.assertRaises(lf.LastfmError):
            lf.top_artists(c, USER, now=datetime.datetime(2026, 1, 1))
        self.assertEqual(c.calls, 0)


class ClientTests(unittest.TestCase):
    def test_pacing_between_calls(self):
        c, clk = make(Fake([]))
        for _ in range(3):
            c.call("user.getWeeklyChartList", user=USER)
        self.assertEqual(clk.sleeps, [1.0, 1.0])  # never before the first call, 1 s between the next ones

    def test_pacing_counts_elapsed_time(self):
        c, clk = make(Fake([]))
        c.call("user.getWeeklyChartList", user=USER)
        clk.t += 0.4
        c.call("user.getWeeklyChartList", user=USER)
        self.assertEqual(len(clk.sleeps), 1)
        self.assertAlmostEqual(clk.sleeps[0], 0.6)

    def test_default_interval_is_one_second(self):
        self.assertGreaterEqual(lf.MIN_INTERVAL, 1.0)

    def test_only_allowlisted_read_methods(self):
        c, _ = make(Fake([]))
        with self.assertRaises(lf.LastfmError):
            c.call("user.getTopArtists", user=USER)
        with self.assertRaises(lf.LastfmError):
            c.call("track.scrobble", user=USER)
        self.assertEqual(c.calls, 0)

    def test_request_url(self):
        fake = Fake([])
        c, _ = make(fake)
        c.call("user.getWeeklyChartList", user=USER)
        u = urllib.parse.urlsplit(fake.urls[0])
        self.assertEqual((u.scheme, u.netloc, u.path), ("https", "ws.audioscrobbler.com", "/2.0/"))

    def test_mapped_error_codes_and_redaction(self):
        for code, text in ((6, "user not found"), (10, "LASTFM_API_KEY"), (17, "private"), (26, "suspended")):
            c, _ = make(Fake([], errors=[(200, {"error": code, "message": f"x {KEY} y"})]))
            with self.assertRaises(lf.LastfmError) as cm:
                c.call("user.getWeeklyChartList", user=USER)
            self.assertIn(text, str(cm.exception))
            self.assertNotIn(KEY, str(cm.exception))
            self.assertEqual(c.calls, 1)  # not retried

    def test_error_29_retries_then_succeeds_with_backoff(self):
        c, clk = make(Fake([], errors=[(200, {"error": 29}), (200, {"error": 29})]))
        c.call("user.getWeeklyChartList", user=USER)
        self.assertEqual(c.calls, 3)
        self.assertIn(5.0, clk.sleeps)
        self.assertIn(10.0, clk.sleeps)

    def test_error_29_gives_up_bounded(self):
        c, _ = make(Fake([], errors=[(200, {"error": 29})] * 10))
        with self.assertRaisesRegex(lf.LastfmError, "rate limit"):
            c.call("user.getWeeklyChartList", user=USER)
        self.assertEqual(c.calls, lf.MAX_RETRIES + 1)

    def test_error_8_retries_then_succeeds(self):
        c, clk = make(Fake([], errors=[(200, {"error": 8, "message": "Operation failed"})] * 2))
        c.call("user.getWeeklyChartList", user=USER)
        self.assertEqual(c.calls, 3)
        self.assertEqual([s for s in clk.sleeps if s >= 5.0], [5.0, 10.0])  # the same backoff as error 29

    def test_error_8_forever_raises_bounded_naming_the_method(self):
        c, _ = make(Fake([], errors=[(200, {"error": 8, "message": f"Operation failed {KEY} {USER}"})] * 20))
        with self.assertRaises(lf.LastfmError) as cm:
            c.call("user.getWeeklyChartList", user=USER)
        msg = str(cm.exception)
        self.assertIn("error 8 in user.getWeeklyChartList", msg)
        self.assertIn(f"after {lf.MAX_RETRIES} retries", msg)
        self.assertEqual(c.calls, lf.MAX_RETRIES + 1)

    def test_error_8_retries_count_in_the_budget(self):
        c, _ = make(Fake([], errors=[(200, {"error": 8})] * 20), max_calls=2)
        with self.assertRaisesRegex(lf.LastfmError, "call limit"):
            c.call("user.getWeeklyChartList", user=USER)
        self.assertEqual(c.calls, 2)
        c, _ = make(Fake([], errors=[(200, {"error": 8})] * 20))
        with c.call_budget(3), self.assertRaisesRegex(lf.LastfmError, "call limit"):
            c.call("user.getWeeklyChartList", user=USER)
        self.assertEqual(c.calls, 3)

    def test_other_codes_are_still_not_retried_and_name_the_method(self):
        for code in (6, 10, 17, 26, 99):
            c, _ = make(Fake([], errors=[(200, {"error": code})]))
            with self.assertRaisesRegex(lf.LastfmError, "in user.getWeeklyChartList"):
                c.call("user.getWeeklyChartList", user=USER)
            self.assertEqual(c.calls, 1)

    def test_error_29_exhaustion_names_the_method(self):
        c, _ = make(Fake([], errors=[(200, {"error": 29})] * 10))
        with self.assertRaisesRegex(lf.LastfmError, "rate limit exceeded .* in user.getWeeklyChartList after 3 retries"):
            c.call("user.getWeeklyChartList", user=USER)

    def test_week_ordinal_without_dates(self):
        w1, w2 = week_ending(NOW, 2), week_ending(NOW, 1)
        errs = [(200, chart_list([w1, w2])), (200, artist_chart([]))] + [(200, {"error": 8})] * 4
        c, _ = make(Fake([w1, w2], errors=errs))
        with self.assertRaises(lf.LastfmError) as cm:
            lf.top_artists(c, USER, 5, NOW)
        msg = str(cm.exception)
        self.assertIn("error 8 in user.getWeeklyArtistChart (week 2 of 2)", msg)
        for ts in (*w1, *w2):
            self.assertNotIn(str(ts), msg)
        self.assertEqual(c.calls, 2 + lf.MAX_RETRIES + 1)  # list + week 1 + week 2 with its retries

    def test_error_text_never_leaks_key_user_or_url(self):
        for answer in ({"error": 8}, {"error": 99, "message": f"{KEY} {USER} {KEY.lower()} {USER.upper()}"},
                       {"error": 6}):
            c, _ = make(Fake([], errors=[(200, answer)] * 10))
            with self.assertRaises(lf.LastfmError) as cm:
                c.call("user.getWeeklyArtistChart", week=(3, 9), user=USER, **{"from": "1", "to": "2"})
            msg = str(cm.exception)
            for secret in (KEY, KEY.lower(), USER, USER.upper(), "api_key", "audioscrobbler", "https://"):
                self.assertNotIn(secret, msg)
            self.assertIn("(week 3 of 9)", msg)
            self.assertLess(len(msg), 400)

    def test_http_5xx_and_network_errors_retry(self):
        c, _ = make(Fake([], errors=[(503, {}), lf.LastfmError("network error talking to Last.fm (TimeoutError)")]))
        c.call("user.getWeeklyChartList", user=USER)
        self.assertEqual(c.calls, 3)
        c, _ = make(Fake([], errors=[(429, {}), (200, {"weeklychartlist": {}})]))  # HTTP 429 is retried too
        c.call("user.getWeeklyChartList", user=USER)
        self.assertEqual(c.calls, 2)
        c, _ = make(Fake([], errors=[(502, {})] * 10))
        with self.assertRaisesRegex(lf.LastfmError, "HTTP 502"):
            c.call("user.getWeeklyChartList", user=USER)
        self.assertEqual(c.calls, lf.MAX_RETRIES + 1)

    def test_http_4xx_empty_body_and_malformed_error(self):
        for answer, text in (((404, {}), "HTTP 404"), ((200, {}), "empty"), ((200, {"error": "x"}), "malformed"),
                             ((200, {"error": True}), "malformed")):
            c, _ = make(Fake([], errors=[answer]))
            with self.assertRaisesRegex(lf.LastfmError, text):
                c.call("user.getWeeklyChartList", user=USER)

    def test_unknown_code_message_is_cleaned_bounded_redacted(self):
        c, _ = make(Fake([], errors=[(200, {"error": 99, "message": "bad\x1b[0m\n" + KEY.lower() + "z" * 500})]))
        with self.assertRaises(lf.LastfmError) as cm:
            c.call("user.getWeeklyChartList", user=USER)
        msg = str(cm.exception)
        self.assertNotIn(KEY.lower(), msg.lower())
        self.assertNotIn("\x1b", msg)
        self.assertLess(len(msg), 300)

    def test_call_budget(self):
        c, _ = make(Fake([]), max_calls=2)
        c.call("user.getWeeklyChartList", user=USER)
        c.call("user.getWeeklyChartList", user=USER)
        with self.assertRaisesRegex(lf.LastfmError, "call limit"):
            c.call("user.getWeeklyChartList", user=USER)
        self.assertEqual(c.calls, 2)

    def test_budget_stops_a_ranking(self):
        ranges = [week_ending(NOW, i) for i in range(1, 11)]
        c, _ = make(Fake(ranges), max_calls=4)
        with self.assertRaisesRegex(lf.LastfmError, "call limit"):
            lf.top_artists(c, USER, 5, NOW)

    def test_key_validation_and_env(self):
        for bad in ("", "short", "has space 0123456789abcdef", 5, None):
            with self.assertRaises(lf.LastfmError) as cm:
                lf.LastfmClient(bad)
            self.assertNotIn("has space", str(cm.exception))
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(lf.LastfmError, "LASTFM_API_KEY is not set"):
                lf.api_key_from_env()
        with mock.patch.dict(os.environ, {"LASTFM_API_KEY": "bad key value"}, clear=True):
            with self.assertRaises(lf.LastfmError) as cm:
                lf.api_key_from_env()
            self.assertNotIn("bad key value", str(cm.exception))
        with mock.patch.dict(os.environ, {"LASTFM_API_KEY": KEY}, clear=True):
            self.assertEqual(lf.api_key_from_env(), KEY)

    def test_transport_error_never_shows_the_key(self):
        def boom(url, timeout):
            raise lf.LastfmError(f"network error {url}")  # even a careless inner message is redacted on the way out
        c, _ = make(boom)
        with self.assertRaises(lf.LastfmError) as cm:
            c.call("user.getWeeklyChartList", user=USER)
        self.assertNotIn(KEY, str(cm.exception))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/elsewhere?" + self.path.split("?", 1)[-1])
            self.end_headers()
        elif self.path.startswith("/big"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{" + b" " * (lf.MAX_BODY + 10) + b"}")
        elif self.path.startswith("/deep"):  # deeply nested JSON that fits under the size cap
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"[" * (lf.MAX_BODY - 10))
        elif self.path.startswith("/err"):
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b'{"error": 10, "message": "Invalid API key"}')
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok": 1}')


class DefaultFetchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_ok(self):
        self.assertEqual(lf.default_fetch(self.base + "/", 5), (200, {"ok": 1}))

    def test_error_body_is_parsed(self):
        self.assertEqual(lf.default_fetch(self.base + "/err", 5)[0], 403)
        self.assertEqual(lf.default_fetch(self.base + "/err", 5)[1]["error"], 10)

    def test_redirect_not_followed_and_key_not_leaked(self):
        status, body = lf.default_fetch(self.base + f"/redirect?api_key={KEY}", 5)
        self.assertEqual((status, body), (302, {}))

    def test_oversized_body_rejected(self):
        with self.assertRaisesRegex(lf.LastfmError, "larger than"):
            lf.default_fetch(self.base + "/big", 5)

    def test_deeply_nested_body_is_a_clean_error(self):
        self.assertEqual(lf.default_fetch(self.base + "/deep", 10), (200, {}))  # no RecursionError escapes
        calls = []

        def fetch(url, timeout):
            calls.append(url)
            return lf.default_fetch(self.base + "/deep", timeout)
        c, _ = make(fetch)
        with self.assertRaisesRegex(lf.LastfmError, "empty or non-JSON"):
            c.call("user.getWeeklyChartList", user=USER)
        self.assertEqual(len(calls), 1)

    def test_oversized_body_is_not_retried(self):
        calls = []

        def fetch(url, timeout):
            calls.append(url)
            return lf.default_fetch(self.base + "/big", timeout)
        c, _ = make(fetch)
        with self.assertRaisesRegex(lf.LastfmError, "larger than"):
            c.call("user.getWeeklyChartList", user=USER)
        self.assertEqual(len(calls), 1)

    def test_cli_deeply_nested_body_prints_error_not_traceback(self):
        def factory(key):
            return REAL_CLIENT(key, fetch=lambda url, t: lf.default_fetch(self.base + "/deep", t),
                               sleep=lambda s: None, out=lambda s: None)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"LASTFM_API_KEY": KEY}, clear=True), \
                mock.patch.object(lf.tg_check, "load_env"), mock.patch.object(lf, "LastfmClient", factory), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = lf.main(["top-artists", "--user", USER])
        self.assertEqual(rc, 1)
        self.assertIn("empty or non-JSON", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_network_error_shows_class_name_only(self):
        with self.assertRaises(lf.LastfmError) as cm:
            lf.default_fetch(f"http://127.0.0.1:1/?api_key={KEY}", 2)
        self.assertNotIn(KEY, str(cm.exception))
        self.assertIn("network error", str(cm.exception))


class ProbeAndCliTests(unittest.TestCase):
    def test_probe_equal_and_different(self):
        a, b = week_ending(NOW, 2), week_ending(NOW, 1)
        wide = (a[0], b[1])
        for wide_ans, expect in ((artist_chart([("X", None, 3)]), True), (artist_chart([("X", None, 2)]), False)):
            fake = Fake([a, b], {a: artist_chart([("X", None, 1)]), b: artist_chart([("X", None, 2)]), wide: wide_ans})
            c, _ = make(fake)
            self.assertEqual(lf.probe_range(c, USER, NOW)[0], expect)
            self.assertEqual(c.calls, 4)

    def test_probe_needs_adjacent_weeks(self):
        c, _ = make(Fake([week_ending(NOW, 9), week_ending(NOW, 1)]))
        with self.assertRaisesRegex(lf.LastfmError, "adjacent"):
            lf.probe_range(c, USER, NOW)

    def run_cli(self, argv, env, client_factory=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(lf.tg_check, "load_env"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if client_factory:
                with mock.patch.object(lf, "LastfmClient", client_factory):
                    rc = lf.main(argv)
            else:
                rc = lf.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_cli_missing_key(self):
        rc, out, err = self.run_cli(["top-artists", "--user", USER], {})
        self.assertEqual(rc, 1)
        self.assertIn("LASTFM_API_KEY is not set", err)

    def test_cli_prints_names_not_key(self):
        w = week_ending(datetime.datetime.now(datetime.timezone.utc), 1)
        fake = Fake([w], {w: artist_chart([("Alpha", None, 4), ("Beta", None, 9)])})
        clk = Clock()

        def factory(key):
            return REAL_CLIENT(key, fetch=fake, sleep=clk.sleep, clock=clk.now, out=lambda s: None)
        rc, out, err = self.run_cli(["top-artists", "--user", USER, "--max", "2"], {"LASTFM_API_KEY": KEY}, factory)
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), ["1\tBeta\t9", "2\tAlpha\t4"])
        self.assertNotIn(KEY, out + err)

    def test_cli_error_is_redacted(self):
        fake = Fake([], errors=[(200, {"error": 10})])

        def factory(key):
            return REAL_CLIENT(key, fetch=fake, sleep=lambda s: None, out=lambda s: None)
        rc, out, err = self.run_cli(["top-artists", "--user", USER], {"LASTFM_API_KEY": KEY}, factory)
        self.assertEqual(rc, 1)
        self.assertIn("invalid API key", err)
        self.assertNotIn(KEY, out + err)


if __name__ == "__main__":
    unittest.main()
