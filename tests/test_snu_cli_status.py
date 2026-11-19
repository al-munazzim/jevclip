"""CLI semantics for directed SNU search and unresolved length constraints."""
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from jevclip import cli


class SNUCLIStatusTests(unittest.TestCase):
    def result(self, **changes):
        data = {
            "candidates": 2, "cuts": 0, "snus": 1,
            "usage": {"requests": 0, "input_tokens": 0, "usd": 0.0},
            "reused": 0, "errors": [], "unresolved": 1,
            "oversized_unresolved": 1, "forced": 0,
            "folder": "unused",
        }
        data.update(changes)
        return data

    def run_cli(self, args, result):
        with mock.patch.object(cli.pipeline, "discover", return_value=[(None, "v.srt")]), \
             mock.patch.object(cli.snu, "process", return_value=result) as process:
            code = cli.main(["--db", ":memory:", "snu", "v.srt", *args])
        return code, process

    def test_explicit_max_with_unresolved_unit_is_nonzero(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code, process = self.run_cli(["--max-snu-seconds", "90"], self.result())
        self.assertEqual(code, 1)
        self.assertIn("1 failed/skipped/incomplete", output.getvalue())
        self.assertFalse(process.call_args.kwargs["force_max_snu"])
        self.assertEqual(process.call_args.kwargs["search_budget"], 8)
        self.assertEqual(process.call_args.kwargs["min_snu_seconds"], 12.0)

    def test_no_max_still_reports_uncertain_unit_as_incomplete(self):
        code, _ = self.run_cli([], self.result(oversized_unresolved=0))
        self.assertEqual(code, 1)

    def test_uncertain_but_short_unit_is_not_an_oversized_failure(self):
        code, _ = self.run_cli(["--max-snu-seconds", "90"],
                               self.result(oversized_unresolved=0))
        self.assertEqual(code, 1)

    def test_explicit_force_acknowledges_remaining_uncertainty(self):
        code, process = self.run_cli(
            ["--max-snu-seconds", "90", "--force-max-snu", "--search-budget", "4"],
            self.result(unresolved=1, oversized_unresolved=0, forced=1, cuts=1, snus=2),
        )
        self.assertEqual(code, 0)
        self.assertTrue(process.call_args.kwargs["force_max_snu"])
        self.assertEqual(process.call_args.kwargs["search_budget"], 4)

    def test_force_requires_positive_max_and_budget(self):
        self.assertEqual(cli.main(["--db", ":memory:", "snu", "v.srt", "--force-max-snu"]), 2)
        self.assertEqual(cli.main(["--db", ":memory:", "snu", "v.srt", "--search-budget", "0"]), 2)
        self.assertEqual(cli.main(["--db", ":memory:", "snu", "v.srt", "--max-snu-seconds", "-1"]), 2)
        self.assertEqual(cli.main(["--db", ":memory:", "snu", "v.srt", "--max-snu-seconds", "nan"]), 2)
        self.assertEqual(cli.main(["--db", ":memory:", "snu", "v.srt", "--min-snu-seconds", "-1"]), 2)

    def test_ensure_partition_accepts_degraded_valid_result_and_passes_margin(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code, process = self.run_cli(['--ensure-partition', '--max-snu-seconds', '8',
                                          '--timing-margin', '.2'],
                                         self.result(partition_valid=True, forced=1, cuts=1, snus=2,
                                                     unresolved=0, oversized_unresolved=0, errors=['timeout']))
        self.assertEqual(code, 0)
        self.assertEqual(process.call_args.kwargs['timing_margin'], .2)
        self.assertTrue(process.call_args.kwargs['ensure_partition'])
        self.assertIn('fallback cut', output.getvalue())
        self.assertNotIn('explicitly forced', output.getvalue())

    def test_ensure_partition_rejects_bad_options(self):
        for args in (['--ensure-partition'], ['--ensure-partition', '--max-snu-seconds', 'nan'],
                     ['--ensure-partition', '--max-snu-seconds', '2', '--timing-margin', '1'],
                     ['--timing-margin', '.2']):
            self.assertEqual(cli.main(['--db', ':memory:', 'snu', 'v.srt', *args]), 2)

    def test_batch_fails_if_one_partition_cannot_be_validated(self):
        with mock.patch.object(cli.pipeline, 'discover', return_value=[(None, 'a.srt'), (None, 'b.srt')]), \
             mock.patch.object(cli.snu, 'process', side_effect=[
                 self.result(partition_valid=True, errors=[], unresolved=0, oversized_unresolved=0),
                 ValueError('impossible cap')]):
            code = cli.main(['--db', ':memory:', 'snu', 'a.srt', 'b.srt',
                             '--ensure-partition', '--max-snu-seconds', '10'])
        self.assertEqual(code, 1)


    def test_skipped_video_removes_stale_opt_in_partition(self):
        from jevclip.store import slug
        with tempfile.TemporaryDirectory() as d:
            video = str(Path(d) / 'missing.mp4')
            folder = Path(d) / (slug(video) + '-partition')
            folder.mkdir()
            (folder / 'snus.json').write_text('stale')
            (folder / 'snu-report.md').write_text('stale')
            with mock.patch.object(cli.pipeline, 'discover', return_value=[(video, None)]):
                code = cli.main(['--db', ':memory:', 'snu', video, '--out', d,
                                 '--ensure-partition', '--max-snu-seconds', '10'])
            self.assertEqual(code, 1)
            self.assertFalse(folder.exists())


if __name__ == "__main__":
    unittest.main()
