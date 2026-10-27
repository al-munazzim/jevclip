"""Offline end-to-end preview, with a fake caption downloader and judge."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.request import urlopen

from jevclip import cli, snu



class PreviewTests(unittest.TestCase):
    def test_caption_language_defaults_to_german_and_can_be_selected(self):
        from jevclip.preview import fetch_captions
        with tempfile.TemporaryDirectory() as d:
            def fake_run(command, **kwargs):
                lang = command[command.index('--sub-langs') + 1]
                Path(command[command.index('--output') + 1] + '.' + lang + '.vtt').write_text('WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHi\n')
                return mock.Mock(returncode=0, stderr='')
            with mock.patch('jevclip.preview.subprocess.run', side_effect=fake_run) as runner:
                self.assertTrue(fetch_captions('Vp4LcxHoojI', d).endswith('.de.vtt'))
                self.assertTrue(fetch_captions('Vp4LcxHoojI', d, language='en').endswith('.en.vtt'))
                self.assertTrue(fetch_captions('Vp4LcxHoojI', d).endswith('.de.vtt'))
            self.assertEqual(runner.call_count, 2)
            with self.assertRaisesRegex(ValueError, 'language'):
                fetch_captions('Vp4LcxHoojI', d, language='../en')

    def test_failed_caption_download_cannot_poison_cache(self):
        from jevclip.preview import fetch_captions
        with tempfile.TemporaryDirectory() as d:
            def partial(command, **kwargs):
                Path(command[command.index('--output') + 1] + '.de.vtt').write_text('partial')
                return mock.Mock(returncode=1, stderr='download failed')
            with mock.patch('jevclip.preview.subprocess.run', side_effect=partial):
                with self.assertRaisesRegex(RuntimeError, 'could not fetch'):
                    fetch_captions('Vp4LcxHoojI', d)
            self.assertFalse((Path(d) / '.captions' / 'Vp4LcxHoojI.de.vtt').exists())

    def test_url_preview_downloads_captions_once_and_renders_partition(self):
        with tempfile.TemporaryDirectory() as d:
            bin_dir = Path(d) / 'bin'
            bin_dir.mkdir()
            executable = bin_dir / 'yt-dlp'
            executable.write_text('#!/usr/bin/env python3\nimport os,sys\nfrom pathlib import Path\n'
                                  'a=sys.argv\nassert "--skip-download" in a and "--no-playlist" in a\n'
                                  'p=Path(a[a.index("--output")+1]+".de.vtt")\n'
                                  'p.write_text("WEBVTT\\n\\n00:00:00.000 --> 00:00:04.000\\n&lt;img src=x onerror=alert(1)&gt; alpha\\n\\n00:00:05.000 --> 00:00:09.000\\nbeta\\n")\n'
                                  'with open(os.environ["FAKE_DLP_LOG"],"a") as f:f.write("call\\n")\n')
            executable.chmod(0o755)
            env = {'PATH': str(bin_dir) + os.pathsep + os.environ.get('PATH', ''),
                   'FAKE_DLP_LOG': str(Path(d) / 'calls')}
            out = Path(d) / 'out'
            args = ['--db', str(Path(d) / 'cache.db'), 'preview',
                    'https://youtu.be/Vp4LcxHoojI', '--max-snu-seconds', '5',
                    '--out', str(out), '--no-serve']
            with mock.patch.dict(os.environ, env), mock.patch.object(snu, 'judge_boundaries', return_value=[]):
                for _ in range(2):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(cli.main(args), 0)
            self.assertEqual((Path(d) / 'calls').read_text().count('call'), 1)
            folder = next(out.glob('*-partition'))
            data = json.loads((folder / 'snus.json').read_text())
            page = (folder / 'preview.html').read_text()
            self.assertEqual(data['schema_version'], 2)
            self.assertIn('youtube-nocookie.com/embed/Vp4LcxHoojI', page)
            self.assertIn('https://www.youtube.com/watch?v=Vp4LcxHoojI&amp;t=0s', page)
            self.assertIn('start=0&amp;end=4', page)
            self.assertIn('best_effort_pause', page)
            self.assertIn('output_timestamps_only_not_measured_speech', page)
            self.assertIn('boundary_scores', page)
            self.assertIn('&quot;threshold&quot;: 0.6', page)
            self.assertIn('alpha', page)
            self.assertNotIn('<img src=x', page)

    def test_local_video_no_serve_and_loopback_page(self):
        with tempfile.TemporaryDirectory() as d:
            video = Path(d) / 'clip.mp4'
            video.write_bytes(b'fake video bytes')
            subs = Path(d) / 'clip.srt'
            subs.write_text('1\n00:00:00,000 --> 00:00:02,000\nHello\n')
            out = Path(d) / 'out'
            with mock.patch.object(snu, 'judge_boundaries', return_value=[]):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(['--db', str(Path(d) / 'db'), 'preview', str(video),
                                               '--subs', str(subs), '--out', str(out), '--no-serve']), 0)
            page = next(out.glob('*-partition/preview.html'))
            self.assertIn('<video', page.read_text())
            self.assertIn('file://', page.read_text())
            from jevclip.preview import make_server
            server = make_server(page, video)
            try:
                import threading
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                base = 'http://127.0.0.1:%d' % server.server_port
                with urlopen(base + '/') as response:
                    self.assertIn(b'<video', response.read())
                with urlopen(base + '/video') as response:
                    self.assertEqual(response.read(), b'fake video bytes')
                from urllib.request import Request
                with urlopen(Request(base + '/video', headers={'Range': 'bytes=0-3'})) as response:
                    self.assertEqual(response.status, 206)
                    self.assertEqual(response.read(), b'fake')
                with urlopen(Request(base + '/video', headers={'Range': 'bytes=-4'})) as response:
                    self.assertEqual(response.status, 206)
                    self.assertEqual(response.read(), b'fake video bytes'[-4:])
                from urllib.error import HTTPError
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + '/other')
                self.assertEqual(error.exception.code, 404)
                error.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_failed_preview_preflight_clears_previous_page(self):
        with tempfile.TemporaryDirectory() as d:
            video = Path(d) / 'clip.mp4'
            video.write_bytes(b'video')
            subs = Path(d) / 'clip.srt'
            subs.write_text('1\n00:00:00,000 --> 00:00:02,000\nHello\n')
            args = ['--db', str(Path(d) / 'db'), 'preview', str(video), '--subs', str(subs),
                    '--out', str(Path(d) / 'out'), '--no-serve']
            with mock.patch.object(snu, 'judge_boundaries', return_value=[]):
                self.assertEqual(cli.main(args), 0)
            from jevclip.store import slug
            folder = Path(d) / 'out' / (slug(str(video)) + '-partition')
            self.assertTrue((folder / 'preview.html').exists())
            subs.write_text('An untimed script, not captions.')
            self.assertNotEqual(cli.main(args), 0)
            self.assertFalse((folder / 'preview.html').exists())

    def test_cli_names_missing_jev_key_without_treating_partition_as_failure(self):
        with tempfile.TemporaryDirectory() as d:
            video = Path(d) / 'clip.mp4'
            video.write_bytes(b'video')
            subs = Path(d) / 'clip.srt'
            subs.write_text('1\n00:00:00,000 --> 00:00:02,000\nAlpha\n\n'
                            '2\n00:00:03,000 --> 00:00:05,000\nBeta\n')
            output = io.StringIO()
            with mock.patch.dict(os.environ, {'TYPESAFE_API_KEY': ''}), contextlib.redirect_stdout(output):
                code = cli.main(['--db', str(Path(d) / 'db'), 'preview', str(video),
                                 '--subs', str(subs), '--out', str(Path(d) / 'out'),
                                 '--max-snu-seconds', '3', '--no-serve'])
            self.assertEqual(code, 0)
            self.assertIn('no_api_key', output.getvalue())
            self.assertIn('Fallback', output.getvalue())

    def test_invalid_input_and_cap_do_not_download(self):
        with tempfile.TemporaryDirectory() as d:
            for url in ('https://example.com/watch?v=Vp4LcxHoojI',
                        'https://youtu.be/bad', 'https://youtu.be/Vp4LcxHoojI?x=evil'):
                self.assertEqual(cli.main(['--db', str(Path(d) / 'db'), 'preview', url,
                                           '--no-serve']), 2)
            self.assertEqual(cli.main(['--db', str(Path(d) / 'db'), 'preview',
                                       'https://youtu.be/Vp4LcxHoojI', '--max-snu-seconds', 'nan',
                                       '--no-serve']), 2)

    def test_render_escapes_untrusted_title_text_and_verdict(self):
        from jevclip.preview import render
        data = {'title': '<script>alert(1)</script>', 'schema_version': 2,
                'doc_id': 'demo', 'strategy': 'snu-partition', 'rubric': 'demo',
                'policy': {}, 'partition': {}, 'normalization': {}, 'usage': {},
                'snus': [{'id': 'SNU1', 'start': 0.2, 'end': 1.3,
                          'text': '<img src=x onerror=alert(1)> & text',
                          'quality_status': 'degraded', 'timing_basis': 'interpolated_cue',
                          'source_timing_basis': 'source_cue', 'classification_status': 'skipped'}],
                'cuts': [], 'boundary_scores': [{'reasons': ['<svg onload=alert(1)>']}]}
        page = render(data, video_id='Vp4LcxHoojI')
        self.assertNotIn('<script>alert(1)</script>', page)
        self.assertNotIn('<img src=x', page)
        self.assertNotIn('<svg onload', page)
        self.assertIn('&lt;img src=x', page)
        self.assertIn('&amp; text', page)
        self.assertIn('start=0&amp;end=2', page)

    def test_quality_overview_explains_fallbacks_without_calling_valid_result_bad(self):
        from jevclip.preview import render
        data = {'title': 'Demo', 'schema_version': 2, 'doc_id': 'demo',
                'strategy': 'snu-partition', 'rubric': 'demo', 'policy': {},
                'partition': {'status': 'valid', 'fallback_cuts': 1,
                              'accepted_semantic_cuts': 0, 'timing_basis': 'source_cue'},
                'normalization': {}, 'usage': {},
                'snus': [{'id': 'SNU1', 'start': 0, 'end': 2, 'text': 'Hallo',
                          'quality_status': 'degraded', 'timing_basis': 'source_cue',
                          'source_timing_basis': 'source_cue', 'classification_status': 'skipped'}],
                'cuts': [{'provenance': 'best_effort_pause', 'time': 2, 'reason': 'hard cap',
                          'timing_basis': 'source_cue', 'judgment': None}],
                'boundary_scores': [{'status': 'error', 'error': 'no_api_key'}]}
        page = render(data, video_id='Vp4LcxHoojI')
        self.assertIn('Partition gültig', page)
        self.assertIn('1 Fallback-Schnitt', page)
        self.assertIn('Jev-Urteil fehlgeschlagen', page)
        self.assertIn('no_api_key', page)
        self.assertIn('nicht, dass die Partition ungültig', page)
        self.assertIn('best_effort_judged', page)
        self.assertIn('best_effort_within_cue', page)
        self.assertIn('Untertitelpause', page)
        self.assertIn('<details', page)
