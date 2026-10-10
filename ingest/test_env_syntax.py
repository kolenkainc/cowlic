"""Cross-check of the .env parser against the shared fixtures in scripts/testdata/env-syntax.

scripts/test-sync-secrets.sh and scripts/test-check-local-prereqs.sh run the same fixtures through the two
shell parsers, so the three implementations cannot drift apart silently.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tg_check

FIXTURES = Path(__file__).resolve().parent.parent / "scripts" / "testdata" / "env-syntax"
KEY = "TG_BOT_TOKEN"


def expectation(path: Path):
    """Returns None for 'expect-invalid', else the expected value of TG_BOT_TOKEN."""
    first = path.read_text(encoding="utf-8").split("\n", 1)[0]
    if first == "# expect-invalid":
        return None
    assert first.startswith("# expect-value:"), f"{path.name}: bad expectation line"
    value = first[len("# expect-value:"):]
    return value[1:] if value.startswith(" ") else value


class EnvSyntaxCrossCheck(unittest.TestCase):
    def test_fixtures_exist(self):
        self.assertGreaterEqual(len(list(FIXTURES.glob("*.env"))), 10)

    def test_every_fixture(self):
        for path in sorted(FIXTURES.glob("*.env")):
            with self.subTest(fixture=path.name):
                expected = expectation(path)
                text = path.read_text(encoding="utf-8")
                if expected is None:
                    with self.assertRaises(tg_check.EnvSyntaxError):
                        tg_check.parse_env(text)
                else:
                    self.assertEqual(tg_check.parse_env(text)[KEY], expected)

    def test_crlf_line_endings(self):
        self.assertEqual(tg_check.parse_env("TG_BOT_TOKEN=fake-abc\r\nX=1\r\n"), {KEY: "fake-abc", "X": "1"})

    def test_error_names_line_and_never_the_value(self):
        with self.assertRaises(tg_check.EnvSyntaxError) as cm:
            tg_check.parse_env("A=1\nTG_BOT_TOKEN=SECRETVALUE # note\n")
        self.assertIn("line 2", str(cm.exception))
        self.assertNotIn("SECRETVALUE", str(cm.exception))

    def test_load_env_keeps_existing_environment(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text("FAKE_A=file\nFAKE_B=file\n", encoding="utf-8")
            fake_module_file = Path(d) / "ingest" / "tg_check.py"
            with mock.patch.object(tg_check, "__file__", str(fake_module_file)), \
                    mock.patch.dict(os.environ, {"FAKE_A": "shell"}, clear=False):
                os.environ.pop("FAKE_B", None)
                tg_check.load_env()
                self.assertEqual((os.environ["FAKE_A"], os.environ["FAKE_B"]), ("shell", "file"))
                os.environ.pop("FAKE_B", None)


if __name__ == "__main__":
    unittest.main()
