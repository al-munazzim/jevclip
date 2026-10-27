"""Opt-in partition contract; no media-time accuracy claims."""
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jevclip import cli, snu, subtitles
from jevclip.snu_partition import audit_source, partition, validate
from jevclip.store import Store, Transcript
from jevclip.subtitles import Cue
from jevclip.snu import BoundaryVerdict, cue_segments
from jevclip.jev import JevClient, JudgeError


class FailsafeTests(unittest.TestCase):
    def test_judge_unavailable_is_explicit_fallback_not_semantic_success(self):
        with tempfile.TemporaryDirectory() as d, Store(':memory:') as store:
            source = Path(d) / 'talk.json'
            source.write_text(json.dumps([{'start': 0, 'end': 4, 'text': 'alpha'},
                                          {'start': 5, 'end': 9, 'text': 'beta'}]))
            def unavailable(*args):
                raise JudgeError('no_api_key')
            client = JevClient(transport=unavailable, workers=1)
            result = snu.process(store, client, None, str(source), d, classify=False,
                                 ensure_partition=True, max_snu_seconds=5)
            data = json.loads((Path(result['folder']) / 'snus.json').read_text())
            self.assertEqual(data['partition']['status'], 'valid')
            self.assertIn('no_api_key', data['partition']['search_errors'])
            self.assertEqual(data['cuts'][0]['provenance'], 'best_effort_pause')
            self.assertNotEqual(data['cuts'][0]['provenance'], 'accepted_semantic')

    def test_opt_in_process_publishes_versioned_partition_without_classification(self):
        with tempfile.TemporaryDirectory() as d, Store(':memory:') as store:
            source = Path(d) / 'talk.json'
            source.write_text(json.dumps([{'start': 0, 'end': 20.0004, 'text': 'one two three four five six'}]))
            client = mock.Mock(usage={'requests': 0, 'input_tokens': 0, 'usd': 0.0})
            with mock.patch.object(snu, 'judge_boundaries', return_value=[]):
                result = snu.process(store, client, None, str(source), d, classify=False,
                                     ensure_partition=True, max_snu_seconds=8)
            data = json.loads((Path(result['folder']) / 'snus.json').read_text())
            self.assertEqual(data['schema_version'], 2)
            self.assertEqual(data['partition']['status'], 'valid')
            self.assertEqual(data['snus'][-1]['end'], 20.0004)
            self.assertEqual(data['snus'][0]['classification_status'], 'skipped')
            self.assertGreater(data['partition']['affected_units'], 0)
            self.assertGreater(data['partition']['affected_duration_share'], 0)
            self.assertIn('changed_text_cues', data['normalization'])
            self.assertIn('interpolated', (Path(result['folder']) / 'snu-report.md').read_text())

    def test_published_pair_uses_one_atomic_generation_pointer(self):
        with tempfile.TemporaryDirectory() as d, Store(':memory:') as store:
            source = Path(d) / 'talk.json'
            source.write_text(json.dumps([{'start': 0, 'end': 2, 'text': 'hello'}]))
            client = mock.Mock(usage={'requests': 0, 'input_tokens': 0, 'usd': 0.0})
            with mock.patch.object(snu, 'judge_boundaries', return_value=[]):
                result = snu.process(store, client, None, str(source), d, classify=False,
                                     ensure_partition=True, max_snu_seconds=3)
            folder = Path(result['folder'])
            self.assertTrue(folder.is_symlink())
            self.assertEqual((folder / 'snus.json').resolve().parent,
                             (folder / 'snu-report.md').resolve().parent)

    def test_failed_opt_in_run_removes_stale_artifacts(self):
        with tempfile.TemporaryDirectory() as d, Store(':memory:') as store:
            source = Path(d) / 'talk.json'
            source.write_text(json.dumps([{'start': 0, 'end': 20, 'text': 'a'}]))
            # Seed the exact destination via slug, not a guessed folder.
            from jevclip.store import slug
            folder = Path(d) / (slug(str(source)) + '-partition')
            folder.mkdir()
            for name in ('snus.json', 'snu-report.md'):
                (folder / name).write_text('stale')
            client = mock.Mock(usage={'requests': 0, 'input_tokens': 0, 'usd': 0.0})
            with mock.patch.object(snu, 'judge_boundaries', return_value=[]):
                with self.assertRaisesRegex(ValueError, 'cap'):
                    snu.process(store, client, None, str(source), d, classify=False,
                                ensure_partition=True, max_snu_seconds=5)
            self.assertFalse((folder / 'snus.json').exists())
            self.assertFalse((folder / 'snu-report.md').exists())

    def test_classification_failure_does_not_invalidate_partition(self):
        with tempfile.TemporaryDirectory() as d, Store(':memory:') as store:
            source = Path(d) / 'talk.json'
            source.write_text(json.dumps([{'start': 0, 'end': 2, 'text': 'hello'}]))
            client = mock.Mock(usage={'requests': 0, 'input_tokens': 0, 'usd': 0.0})
            def failed(*args, **kwargs):
                for obj in args[3]:
                    obj.error_code = 'timeout'
                return args[3]
            with mock.patch.object(snu, 'judge_boundaries', return_value=[]), \
                 mock.patch.object(snu, 'classify_snus', side_effect=failed):
                result = snu.process(store, client, None, str(source), d,
                                     ensure_partition=True, max_snu_seconds=3)
            data = json.loads((Path(result['folder']) / 'snus.json').read_text())
            self.assertEqual(data['partition']['status'], 'valid')
            self.assertEqual(data['snus'][0]['classification_status'], 'failed')

    def test_start_only_transcript_marks_inferred_timing(self):
        with tempfile.TemporaryDirectory() as d, Store(':memory:') as store:
            source = Path(d) / 'talk.txt'
            source.write_text('[00:00] hello world\n[00:10] next line')
            client = mock.Mock(usage={'requests': 0, 'input_tokens': 0, 'usd': 0.0})
            with mock.patch.object(snu, 'judge_boundaries', return_value=[]):
                result = snu.process(store, client, None, str(source), d, classify=False,
                                     ensure_partition=True, max_snu_seconds=5)
            data = json.loads((Path(result['folder']) / 'snus.json').read_text())
            self.assertEqual(data['partition']['timing_basis'], 'inferred_start_only_end')
            self.assertEqual(data['snus'][0]['timing_basis'], 'inferred_start_only_end')

    def test_interpolated_start_only_cue_retains_underlying_inferred_basis(self):
        t = self.transcript([Cue(0, 12, 'many spaced words to split')])
        units, cuts = partition(t, [], 5, .15, 'inferred_start_only_end')
        self.assertEqual(units[0]['timing_basis'], 'interpolated_cue')
        self.assertEqual(units[0]['source_timing_basis'], 'inferred_start_only_end')
        self.assertEqual(cuts[0]['source_timing_basis'], 'inferred_start_only_end')

    def transcript(self, cues):
        return Transcript('demo', 1, 'demo', None, 'demo.json', '', cues, [], True)

    def test_partition_preserves_semantic_cuts_and_repairs_only_oversized_spans(self):
        t = self.transcript([Cue(i * 4, i * 4 + 3, f'word {i}.') for i in range(6)])
        segments = cue_segments(t)
        accepted = BoundaryVerdict('B3', segments[2], segments[3], 'ok', cut=True, cue_index=3)
        rejected = BoundaryVerdict('B2', segments[1], segments[2], 'ok', cut=False,
                                   cue_index=2, good_boundary=.8)
        units, cuts = partition(t, [accepted, rejected], 9, .15)
        self.assertEqual([c['provenance'] for c in cuts],
                         ['best_effort_judged', 'accepted_semantic', 'best_effort_pause'])
        self.assertEqual([u['text'] for u in units], ['word 0.\nword 1.\n', 'word 2.\n',
                                                     'word 3.\nword 4.\n', 'word 5.\n'])
        validate(t, units, cuts, 9)

    def test_accepted_cut_evidence_wins_over_prior_rejection_at_same_position(self):
        t = self.transcript([Cue(0, 2, 'first'), Cue(2, 4, 'second')])
        cues = cue_segments(t)
        accepted = BoundaryVerdict('B1-new', cues[0], cues[1], 'ok', cut=True, cue_index=1,
                                   good_boundary=.9)
        rejected = BoundaryVerdict('B1-old', cues[0], cues[1], 'ok', cut=False, cue_index=1,
                                   good_boundary=.1)
        _, cuts = partition(t, [accepted, rejected], 5)
        self.assertEqual(cuts[0]['judgment']['id'], 'B1-new')

    def test_unresolved_search_is_not_labeled_semantic_quality(self):
        t = self.transcript([Cue(0, 2, 'first'), Cue(2, 4, 'second')])
        cues = cue_segments(t)
        verdict = BoundaryVerdict('B1', cues[0], cues[1], 'error', cut=False,
                                  cue_index=1, error_code='timeout')
        units, _ = partition(t, [verdict], 5)
        self.assertEqual(units[0]['quality_status'], 'unresolved_search')

    def test_uninspected_multiple_cues_are_not_certified_semantic(self):
        t = self.transcript([Cue(0, 2, 'first'), Cue(2, 4, 'second')])
        units, _ = partition(t, [], 5)
        self.assertEqual(units[0]['quality_status'], 'unresolved_search')

    def test_long_cue_interpolation_covers_text_with_bounded_serialized_times(self):
        text = 'First sentence. Second sentence. ' + 'x' * 80
        t = self.transcript([Cue(0, 31.0001, text)])
        units, cuts = partition(t, [], 10, .15)
        self.assertEqual(''.join(u['text'] for u in units), text)
        self.assertTrue(all(u['timing_basis'] == 'interpolated_cue' for u in units))
        self.assertTrue(all(c['provenance'] == 'best_effort_within_cue' for c in cuts))
        self.assertEqual(units[-1]['end'], 31.0001)
        validate(t, json.loads(json.dumps(units)), cuts, 10)

    def test_planning_margin_uses_relative_overrun_not_cap_discount(self):
        t = self.transcript([Cue(0, 100, 'x' * 100)])
        units, _ = partition(t, [], 90, .15)
        self.assertEqual(len(units[0]['text']), 78)

    def test_gaps_count_and_impossible_grapheme_cap_fails(self):
        t = self.transcript([Cue(0, .5, 'a'), Cue(20, 21, 'b')])
        units, cuts = partition(t, [], 5, .15)
        self.assertEqual(len(units), 2)
        with self.assertRaisesRegex(ValueError, 'cap'):
            partition(self.transcript([Cue(0, 20, 'a')]), [], 5, .15)

    def test_joined_emoji_is_not_split_into_partial_graphemes(self):
        t = self.transcript([Cue(0, 6, '👩\u200d💻')])
        with self.assertRaisesRegex(ValueError, 'grapheme'):
            partition(t, [], 4, 0)

    def test_emoji_modifier_is_not_split_from_its_base(self):
        t = self.transcript([Cue(0, 6, '👍🏽')])
        with self.assertRaisesRegex(ValueError, 'grapheme'):
            partition(t, [], 4, 0)

    def test_chinese_sentence_end_is_preferred_without_whitespace(self):
        t = self.transcript([Cue(0, 10, '你好。世界你好。世界')])
        units, _ = partition(t, [], 5, 0)
        self.assertEqual(units[0]['text'], '你好。')

    def test_validator_rejects_text_tampering_and_marks_both_fallback_sides(self):
        t = self.transcript([Cue(0, 4, 'alpha'), Cue(5, 9, 'beta')])
        units, cuts = partition(t, [], 5, .15)
        self.assertEqual([u['quality_status'] for u in units], ['degraded', 'degraded'])
        units[0]['text'] = 'missing'
        with self.assertRaisesRegex(ValueError, 'text'):
            validate(t, units, cuts, 5)

    def test_start_only_copied_transcript_rejects_decreasing_stamps(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'copied.txt'
            p.write_text('[00:05] first\n[00:03] second\n[00:10] third')
            with self.assertRaisesRegex(ValueError, 'timing'):
                audit_source(str(p))

    def test_raw_timing_audit_rejects_invalid_cues_before_cleaning(self):
        with tempfile.TemporaryDirectory() as d:
            for times in [(2, 1), (0, math.inf), (math.nan, 2)]:
                path = Path(d) / 'source.json'
                path.write_text(json.dumps([{'start': times[0], 'end': times[1], 'text': 'lost'}]))
                with self.assertRaisesRegex(ValueError, 'timing'):
                    audit_source(str(path))
            path.write_text(json.dumps([{'start': 0, 'end': 2, 'text': 'a'},
                                        {'start': 1, 'end': 3, 'text': 'b'}]))
            with self.assertRaisesRegex(ValueError, 'overlap'):
                audit_source(str(path))

if __name__ == '__main__':
    unittest.main()
