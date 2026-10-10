import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tg_check

OK = [
    (b"TG_BOT_TOKEN=fake-abc\n", "fake-abc"),
    (b'TG_BOT_TOKEN="fake abc"\n', "fake abc"),
    (b"TG_BOT_TOKEN='fake abc'\n", "fake abc"),
    (b"# note\n\n   # indented note\nTG_BOT_TOKEN=fake-abc\n\n", "fake-abc"),
    (b"TG_BOT_TOKEN=ab#cd=1\\n$X\n", "ab#cd=1\\n$X"),
    (b"TG_BOT_TOKEN=\n", ""),
    (b'TG_BOT_TOKEN=""\n', ""),
    (b"TG_BOT_TOKEN=fake-abc\r\n", "fake-abc"),
    (b"TG_BOT_TOKEN=fake-abc", "fake-abc"),
    (b"# \xc3\xa9 \xff comment bytes are free\nTG_BOT_TOKEN=fake-abc\n", "fake-abc"),
]
BAD = [
    b"export TG_BOT_TOKEN=fake\n",
    b"TG_BOT_TOKEN=fake # note\n",
    b'TG_BOT_TOKEN="fake" # note\n',
    b'TG_BOT_TOKEN="fake\n',
    b"TG_BOT_TOKEN= fake\n",
    b"TG_BOT_TOKEN = fake\n",
    b"TG_BOT_TOKEN=a\nTG_BOT_TOKEN=b\n",
    b"TG_BOT_TOKEN=fake\njust junk\n",
    b"TG_BOT_TOKEN=fake\rjunk\n",
    b"TG_BOT_TOKEN=abc\r\r\n",
    b"TG_BOT_TOKEN=abc\r",
    b"TG_BOT_TOKEN=fa\x00ke\n",
    b"TG_BOT_TOKEN=fa\xffke\n",
    b"\xc3\xa9=fake\nTG_BOT_TOKEN=abc\n",
]


class ParseEnvTest(unittest.TestCase):
    def test_accepted(self):
        for data, expected in OK:
            with self.subTest(data=data):
                self.assertEqual(tg_check.parse_env(data)["TG_BOT_TOKEN"], expected)

    def test_rejected(self):
        for data in BAD:
            with self.subTest(data=data):
                with self.assertRaises(tg_check.EnvSyntaxError):
                    tg_check.parse_env(data)

    def test_error_names_line_and_never_the_value(self):
        with self.assertRaises(tg_check.EnvSyntaxError) as cm:
            tg_check.parse_env(b"A=1\nTG_BOT_TOKEN=SECRETVALUE # note\n")
        self.assertIn("line 2", str(cm.exception))
        self.assertNotIn("SECRETVALUE", str(cm.exception))

    def _load(self, data: bytes, preset: dict):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".env").write_bytes(data)
            with mock.patch.object(tg_check, "__file__", str(Path(d) / "ingest" / "tg_check.py")), \
                    mock.patch.dict(os.environ, preset):
                os.environ.pop("FAKE_B", None)
                try:
                    tg_check.load_env()
                    return os.environ.get("FAKE_A"), os.environ.get("FAKE_B")
                finally:
                    os.environ.pop("FAKE_B", None)

    def test_load_env_keeps_the_existing_environment(self):
        self.assertEqual(self._load(b"FAKE_A=file\nFAKE_B=file\n", {"FAKE_A": "shell"}), ("shell", "file"))

    def test_load_env_raises_the_clean_error_not_a_crash(self):
        for data in (b"FAKE_A=fa\x00ke\n", b"FAKE_A=fa\xffke\n"):
            with self.subTest(data=data), self.assertRaises(tg_check.EnvSyntaxError):
                self._load(data, {})


if __name__ == "__main__":
    unittest.main()
