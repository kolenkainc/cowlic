import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import bootstrap
import bootstrap_covers as covers
import cover_plan_review as review


class ReviewTests(unittest.TestCase):
    def test_private_names_are_escaped_and_not_mistaken_for_candidate_names(self):
        key = 'a' * 16
        plan = covers.Plan(False, {key: {'rule': 'normalised', 'browse_id': None,
                                       'cover_url': 'https://example.invalid/"<script>'}})
        text = review.render(plan, [covers.Todo(key, '<script>album</script>', 'A&B', 'T', 10)])
        self.assertIn('&lt;script&gt;album&lt;/script&gt;', text)
        self.assertIn('A&amp;B', text)
        self.assertNotIn('<script>', text)
        self.assertIn('candidate name was not retained', text)
        self.assertIn("default-src 'none'", text)
        self.assertNotIn('<img', text)

    def test_replacement_is_private_and_does_not_follow_an_existing_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            target = p / 'unchanged'
            target.write_text('original')
            out = p / review.REVIEW_FILE
            out.symlink_to(target)
            review.write_review(out, 'private')
            self.assertEqual(target.read_text(), 'original')
            self.assertEqual(out.read_text(), 'private')
            self.assertEqual(out.stat().st_mode & 0o777, 0o600)

    def test_main_uses_only_the_existing_read_query_and_leaves_plan_unapproved(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            covers.write_plan(p / covers.PLAN_FILE, {}, bootstrap.today())
            original = (p / covers.PLAN_FILE).read_bytes()
            calls = []
            def query(sql, params):
                calls.append((sql, params))
                return []
            with mock.patch.object(bootstrap, 'state_dir', return_value=p), \
                    mock.patch.object(bootstrap, 'cover_env', return_value=None), \
                    mock.patch.object(bootstrap, 'cover_query', return_value=query), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(review.main(), 0)
            self.assertEqual(calls, [(covers.ALBUMS_SQL, None)])
            self.assertEqual((p / covers.PLAN_FILE).read_bytes(), original)

    def test_lock_prevents_reads(self):
        with mock.patch.object(bootstrap, 'take_lock', side_effect=bootstrap.Locked), \
                mock.patch.object(bootstrap, 'cover_query') as query, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(review.main(), 75)
            query.assert_not_called()

    def test_exception_text_never_reaches_output(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(bootstrap, 'state_dir', return_value=Path(d)), \
                    mock.patch.object(covers, 'load_plan', side_effect=RuntimeError('PRIVATE SECRET')), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(review.main(), 1)
            self.assertNotIn('PRIVATE SECRET', output.getvalue())


if __name__ == '__main__':
    unittest.main()
