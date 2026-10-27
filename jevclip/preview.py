"""Offline-renderable SNU preview; only caption acquisition needs yt-dlp."""
import html
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import snu, subtitles
from .snu_partition import clear_partition_artifacts
from .jev import JevClient


_ID = re.compile(r'^[A-Za-z0-9_-]{11}$')


def youtube_id(source):
    url = urlsplit(source)
    if url.scheme != 'https' or url.username or url.password or url.port or url.fragment:
        raise ValueError('expected a canonical HTTPS YouTube video URL')
    host = url.hostname
    if host == 'youtu.be' and url.path.count('/') == 1 and not url.query:
        video_id = url.path[1:]
    elif host in ('youtube.com', 'www.youtube.com') and url.path == '/watch':
        query = parse_qs(url.query, strict_parsing=True)
        if set(query) != {'v'} or len(query['v']) != 1:
            raise ValueError('expected only one video id (v)')
        video_id = query['v'][0]
    else:
        raise ValueError('expected youtu.be/ID or youtube.com/watch?v=ID')
    if not _ID.fullmatch(video_id):
        raise ValueError('invalid YouTube video id')
    return video_id


def fetch_captions(video_id, out, language='de'):
    if not re.fullmatch(r'[a-z]{2,3}(?:-[a-z]{2,4})?', language):
        raise ValueError('caption language must be a language code such as de or en')
    cache = Path(out) / '.captions'
    cache.mkdir(parents=True, exist_ok=True)
    caption = cache / (video_id + '.' + language + '.vtt')
    if caption.is_file() and subtitles.parse(str(caption)):
        return str(caption)
    with tempfile.TemporaryDirectory(prefix='.' + video_id + '-', dir=cache) as stage:
        base = Path(stage) / video_id
        command = ['yt-dlp', '--no-playlist', '--skip-download', '--write-subs',
                   '--write-auto-subs', '--sub-langs', language, '--sub-format', 'vtt',
                   '--output', str(base), 'https://www.youtube.com/watch?v=' + video_id]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
        except FileNotFoundError as exc:
            raise RuntimeError('yt-dlp executable not found; install yt-dlp to fetch YouTube captions') from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError('yt-dlp timed out while fetching captions') from exc
        candidate = Path(str(base) + '.' + language + '.vtt')
        if completed.returncode or not candidate.is_file() or not subtitles.parse(str(candidate)):
            raise RuntimeError('yt-dlp could not fetch %s VTT captions: %s' %
                               (language, completed.stderr.strip()[-400:]))
        os.replace(candidate, caption)
    return str(caption)


def render(data, video_id=None, video=None):
    esc = lambda value: html.escape(str(value), quote=True)
    blocks = []
    for unit in data['snus']:
        start, end = unit['start'], unit['end']
        if video_id:
            # YouTube's embed start/end parameters take integer seconds; the
            # rounded playback range may extend beyond fractional SNU timestamps.
            src = ('https://www.youtube-nocookie.com/embed/%s?start=%d&end=%d' %
                   (video_id, math.floor(start), math.ceil(end)))
            media = ('<iframe title="%s" src="%s" allow="accelerometer; autoplay; encrypted-media; picture-in-picture" allowfullscreen loading="lazy"></iframe>'
                     '<a href="%s">Watch on YouTube from %d s</a>' %
                     (esc(unit['id']), esc(src),
                      esc('https://www.youtube.com/watch?v=%s&t=%ds' % (video_id, math.floor(start))),
                      math.floor(start)))
        else:
            media = ('<video controls preload="none" data-start="%s" data-end="%s" src="%s"></video>' %
                     (esc(start), esc(end), esc(Path(video).resolve().as_uri())))
        blocks.append('<section><h2>%s — %s to %s</h2>%s<pre>%s</pre><p>Quality: %s; timing: %s; source timing: %s; classification: %s</p></section>' %
                      (esc(unit['id']), esc(start), esc(end), media, esc(unit['text']),
                       esc(unit['quality_status']), esc(unit['timing_basis']),
                       esc(unit['source_timing_basis']), esc(unit['classification_status'])))
    cuts = ''.join('<li>%s at %s: %s (%s; %s)</li>' %
                   (esc(c['provenance']), esc(c['time']), esc(c['reason']),
                    esc(c['timing_basis']), esc(json.dumps(c['judgment'], ensure_ascii=False)))
                   for c in data['cuts'])
    scores = ''.join('<li><pre>%s</pre></li>' % esc(json.dumps(v, ensure_ascii=False, indent=2))
                     for v in data['boundary_scores'])
    metadata = {key: data[key] for key in ('schema_version', 'doc_id', 'strategy', 'rubric',
                 'policy', 'partition', 'normalization', 'usage')}
    script = '''<script>document.querySelectorAll('video').forEach(v=>{
      const a=Number(v.dataset.start), b=Number(v.dataset.end);
      v.addEventListener('loadedmetadata',()=>{v.currentTime=a});
      v.addEventListener('play',()=>{if(v.currentTime<a||v.currentTime>=b)v.currentTime=a});
      v.addEventListener('timeupdate',()=>{if(v.currentTime>=b)v.pause()});
    });</script>''' if video else ''
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><title>SNU preview — %s</title>'
            '<style>body{font:16px system-ui;max-width:900px;margin:auto;padding:2rem}iframe,video{width:100%%;aspect-ratio:16/9}section{border-top:1px solid #bbb;padding:1rem 0}pre{white-space:pre-wrap}</style>'
            '<h1>SNU preview — %s</h1><p>Playback follows output timestamps, not measured speech. '
            'YouTube integer-second embed endpoints can extend beyond fractional SNU bounds.</p>%s'
            '<h2>Cuts and fallback provenance</h2><ol>%s</ol><h2>All boundary verdicts (boundary_scores)</h2><ol>%s</ol>'
            '<h2>Reproducibility: policy, normalization and usage</h2><pre>%s</pre>%s</html>' %
            (esc(data['title']), esc(data['title']), ''.join(blocks), cuts, scores,
             esc(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True)), script))


def make_server(page, video=None):
    page = Path(page)
    video = Path(video) if video else None

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/':
                body = page.read_text(encoding='utf-8')
                if video:
                    body = body.replace(html.escape(video.resolve().as_uri(), quote=True), '/video')
                payload = body.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            elif self.path == '/video' and video:
                size = video.stat().st_size
                range_header = self.headers.get('Range')
                match = re.fullmatch(r'bytes=(\d*)-(\d*)', range_header) if range_header else None
                if range_header and (not match or not (match[1] or match[2])):
                    self.send_error(416)
                    return
                if match and not match[1]:
                    suffix = int(match[2])
                    start, end = max(0, size - suffix), size - 1
                    if suffix == 0:
                        self.send_error(416)
                        return
                else:
                    start = int(match[1]) if match else 0
                    end = min(int(match[2]), size - 1) if match and match[2] else size - 1
                if start >= size or end < start:
                    self.send_error(416)
                    return
                self.send_response(206 if match else 200)
                self.send_header('Content-Type', mimetypes.guess_type(str(video))[0] or 'application/octet-stream')
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Content-Length', str(end - start + 1))
                if match:
                    self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
                self.end_headers()
                with video.open('rb') as stream:
                    stream.seek(start)
                    remaining = end - start + 1
                    while remaining:
                        chunk = stream.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            else:
                self.send_error(404)

    return ThreadingHTTPServer(('127.0.0.1', 0), Handler)


def run(store, args):
    if not math.isfinite(args.max_snu_seconds) or args.max_snu_seconds <= 0 or not math.isfinite(args.threshold) or not 0 <= args.threshold <= 1:
        print('preview requires positive finite --max-snu-seconds and --threshold in [0, 1]', file=sys.stderr)
        return 2
    video_id = None
    video = None
    try:
        if '://' in args.source:
            video_id = youtube_id(args.source)
            if args.subs:
                raise ValueError('--subs applies only to a local video')
            if not re.fullmatch(r'[a-z]{2,3}(?:-[a-z]{2,4})?', args.lang):
                raise ValueError('caption language must be a language code such as de or en')
            caption_path = str(Path(args.out) / '.captions' / (video_id + '.' + args.lang + '.vtt'))
            clear_partition_artifacts(args.out, caption_path)
            subs = fetch_captions(video_id, args.out, args.lang)
        else:
            video = os.path.abspath(args.source)
            clear_partition_artifacts(args.out, video)
            if not os.path.isfile(video):
                raise ValueError('local video not found')
            if not args.subs:
                raise ValueError('local video requires --subs with timed subtitles')
            subs = os.path.abspath(args.subs)
            if not os.path.isfile(subs):
                raise ValueError('subtitle file not found')
        cues = subtitles.parse(subs)
        if not cues or any(c.para is not None for c in cues):
            raise ValueError('preview requires timed captions, not an untimed script')
        client = JevClient(model=args.model)
        try:
            result = snu.process(store, client, video, subs, args.out,
                                 title=video_id if video_id else None,
                                 threshold=args.threshold, max_snu_seconds=args.max_snu_seconds,
                                 ensure_partition=True, classify=False,
                                 preview_renderer=lambda data: render(data, video_id, video))
        finally:
            client.close()
    except (ValueError, RuntimeError, OSError) as exc:
        print('preview failed: %s' % exc, file=sys.stderr)
        return 2 if isinstance(exc, ValueError) else 1
    page = Path(result['folder']) / 'preview.html'
    print('Preview: %s (%d SNUs, %d fallback cuts; Jev %d requests)' %
          (page.absolute(), result['snus'], result['forced'], result['usage']['requests']), flush=True)
    if args.no_serve:
        return 0
    server = make_server(page, video)
    url = 'http://127.0.0.1:%d/' % server.server_port
    print('Serving %s (Ctrl-C to stop)' % url, flush=True)
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
