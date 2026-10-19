import base64
import io
import json
import unittest
import urllib.error
import urllib.parse

import read_grafana_session as reader


class Response(io.BytesIO):
    status = 200


class Opener:
    def __init__(self, body=None, error=None):
        self.body = body
        self.error = error
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        if self.error:
            raise self.error
        return Response(self.body)


def payload(values=None):
    streams = [] if values is None else [{"stream": {"job": "cowlic-bootstrap"}, "values": values}]
    return json.dumps({"status": "success", "data": {"resultType": "streams", "result": streams}}).encode()


class ReadSessionTest(unittest.TestCase):
    def setUp(self):
        self.env = {"INGEST_GRAFANA_LOKI_URL": "https://fixture.grafana.net/loki/api/v1/push",
                    "INGEST_GRAFANA_LOKI_USER": "fixture-user", "INGEST_GRAFANA_TOKEN": "fixture-token"}

    def test_fixed_read_only_query_and_preserved_original_lines(self):
        opener = Opener(payload([["1791799500000000000", "original line"]]))
        data, count = reader.read_session(self.env, opener)
        self.assertEqual(count, 1)
        self.assertEqual(data["data"]["result"][0]["values"][0][1], "original line")
        self.assertEqual(len(opener.requests), 1)
        request = opener.requests[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        url = urllib.parse.urlsplit(request.full_url)
        self.assertEqual(url.netloc, "fixture.grafana.net")
        self.assertEqual(url.path, "/loki/api/v1/query_range")
        self.assertEqual(urllib.parse.parse_qs(url.query), {
            "query": ['{job="cowlic-bootstrap"}'], "start": [reader.START],
            "end": [reader.END], "limit": ["1000"], "direction": ["forward"]})
        self.assertNotIn("fixture-token", request.full_url)
        self.assertTrue(request.get_header("Authorization").startswith("Basic "))

    def test_reject_unsafe_targets_before_sending_credentials(self):
        for url in ("https://evil.example", "http://fixture.grafana.net", "https://u:p@fixture.grafana.net",
                    "https://fixture.grafana.net:8443", "https://fixture.grafana.net/?leak=1"):
            with self.subTest(url=url):
                opener = Opener(payload())
                with self.assertRaises(Exception):
                    reader.read_session({**self.env, "INGEST_GRAFANA_LOKI_URL": url}, opener)
                self.assertEqual(opener.requests, [])

    def test_reject_header_injection_before_request(self):
        opener = Opener(payload())
        with self.assertRaises(Exception):
            reader.read_session({**self.env, "INGEST_GRAFANA_TOKEN": "fixture\r\nX-Evil: yes"}, opener)
        self.assertEqual(opener.requests, [])

    def test_http_failure_is_status_only_and_never_retried(self):
        for status in (302, 401, 403, 429, 503):
            with self.subTest(status=status):
                error = urllib.error.HTTPError("https://private.invalid", status, "fixture-token", {},
                                               io.BytesIO(b"fixture-token"))
                opener = Opener(error=error)
                with self.assertRaisesRegex(reader.ReadError, f"^HTTP {status}$"):
                    reader.read_session(self.env, opener)
                self.assertEqual(len(opener.requests), 1)

    def test_dedicated_read_token_preferred_without_changing_shipping_config(self):
        env = {**self.env, "INGEST_GRAFANA_READ_TOKEN": "fixture-read-only"}
        opener = Opener(payload())
        reader.read_session(env, opener)
        auth = opener.requests[0].get_header("Authorization").split(" ")[1]
        self.assertEqual(base64.b64decode(auth).decode(), "fixture-user:fixture-read-only")
        self.assertEqual(env["INGEST_GRAFANA_TOKEN"], "fixture-token")

    def test_invalid_dedicated_token_cannot_fall_back_to_writer(self):
        opener = Opener(payload())
        with self.assertRaises(Exception):
            reader.read_session({**self.env, "INGEST_GRAFANA_READ_TOKEN": ""}, opener)
        self.assertEqual(opener.requests, [])

    def test_no_raw_network_error_echo(self):
        with self.assertRaisesRegex(reader.ReadError, "^RuntimeError$"):
            reader.read_session(self.env, Opener(error=RuntimeError("fixture-token")))

    def test_response_size_cap(self):
        with self.assertRaisesRegex(reader.ReadError, "^response too large$"):
            reader.read_session(self.env, Opener(b"x" * (reader.MAX_BYTES + 1)))

    def test_full_limit_cannot_be_treated_as_complete_export(self):
        with self.assertRaisesRegex(reader.ReadError, "truncated"):
            reader.read_session(self.env, Opener(payload([["1791799500000000000", "line"]] * reader.LIMIT)))

    def test_empty_retained_range_is_valid_evidence(self):
        self.assertEqual(reader.read_session(self.env, Opener(payload()))[1], 0)

    def test_unexpected_job_rejected(self):
        body = payload([["1791799500000000000", "line"]]).replace(b"cowlic-bootstrap", b"other-job")
        with self.assertRaisesRegex(reader.ReadError, "unexpected stream"):
            reader.read_session(self.env, Opener(body))


if __name__ == "__main__":
    unittest.main()
