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
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import snu, subtitles
from .snu_partition import clear_partition_artifacts
from .jev import JevClient


_ID = re.compile(r'^[A-Za-z0-9_-]{11}$')

_CUT_MEANING = {
    'accepted_semantic': ('Semantisch bestätigt', 'Jev hat den Schnitt nach allen Qualitätsprüfungen akzeptiert.'),
    'best_effort_judged': ('Bewertet, aber nicht akzeptiert', 'Jev hat diese Grenze beurteilt; die Anforderungen für einen semantisch bestätigten Schnitt wurden nicht erfüllt.'),
    'best_effort_pause': ('Untertitelpause', 'Eine zeitliche Pause zwischen zwei Untertitel-Cues wurde als Schnitt genutzt.'),
    'best_effort_sentence': ('Satzende', 'Ein Satzende am Cue-Rand wurde als Schnitt genutzt.'),
    'best_effort_cue': ('Cue-Grenze', 'Eine passende Untertitelgrenze wurde ohne semantische Bestätigung genutzt.'),
    'best_effort_within_cue': ('Innerhalb einer Cue', 'Eine lange Cue wurde im Text geteilt; die Schnittzeit darin ist geschätzt.'),
}

_QUALITY_MEANING = {
    'semantic': 'Keine Fallback-Schnitte in dieser SNU; das sagt nichts über den Inhalt aus.',
    'degraded': 'Mindestens ein mechanischer Fallback-Schnitt berührt diese SNU; sie kann trotzdem gut sein.',
    'unresolved_search': 'Die Suche ist für diese SNU unvollständig; kein Qualitätsurteil über den Inhalt.',
}

_SNU_FIELD_LABELS = {
    'source_start': 'Quellanfang', 'source_stop': 'Quellende',
    'timing_basis': 'Zeitbasis', 'source_timing_basis': 'Quelle der Zeitstempel',
    'quality_status': 'Qualitätsstatus', 'classification_status': 'Klassifikationsstatus',
    'kind': 'Typ', 'kind_confidence': 'Typ-Konfidenz', 'standalone': 'Eigenständig',
    'classification_error': 'Klassifikationsfehler',
}


def _snu_details(unit, esc):
    """Show every SNU JSON field not already visible in the card header/text."""
    fields = [key for key in _SNU_FIELD_LABELS if key in unit]
    fields += sorted(key for key in unit if key not in _SNU_FIELD_LABELS
                     and key not in ('id', 'start', 'end', 'text'))
    rows = []
    for key in fields:
        value = unit[key]
        if key in ('source_start', 'source_stop') and isinstance(value, list) and len(value) == 2:
            display = 'Cue-Index %s, Zeichenoffset %s' % (esc(value[0]), esc(value[1]))
        elif value is None:
            display = '<code>null</code> (nicht gesetzt)'
        elif isinstance(value, bool):
            display = '%s <code>%s</code>' % ('Ja' if value else 'Nein', str(value).lower())
        elif isinstance(value, (dict, list)):
            display = '<code>%s</code>' % esc(json.dumps(value, ensure_ascii=False))
        else:
            display = '<code>%s</code>' % esc(value)
        if key == 'quality_status':
            display += ' — %s' % esc(_QUALITY_MEANING.get(str(value), ''))
        elif key == 'classification_status' and value == 'skipped':
            display += ' — In der Vorschau nicht ausgeführt; unabhängig von der Schnittqualität.'
        rows.append('<div><dt>%s</dt><dd>%s</dd></div>' %
                    (esc(_SNU_FIELD_LABELS.get(key, key)), display))
    return ('<details class="snu-details"><summary>Weitere SNU-Daten '
            '(Quellbereich, Zeitbasis, Status)</summary><p class="note">Quellpositionen beziehen sich '
            'auf normalisierte Untertitel: Cue-Index und Zeichenoffset beginnen bei 0; '
            'der Quellende-Offset ist exklusiv.</p><dl>%s</dl></details>' % ''.join(rows))


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


def _quality_overview(data, esc):
    partition = data['partition']
    cut_counts = Counter(c['provenance'] for c in data['cuts'])
    errors = Counter(v.get('error') or 'unknown' for v in data['boundary_scores']
                     if v.get('status') == 'error')
    error_count = sum(errors.values())
    valid = partition.get('status') == 'valid'
    key_hint = ('<p class="hint">Kein TypeSafe-Key für diesen Lauf: '
                '<code>TYPESAFE_API_KEY</code> exportieren und erneut ausführen. '
                'Ohne Key gibt es keine semantische Bestätigung.</p>' if errors.get('no_api_key') else '')
    error_detail = (', '.join('%s × %d' % (esc(code), count) for code, count in errors.items())
                    if errors else 'keine')
    legend = ''.join('<li><code>%s</code> <strong>%s</strong> — %s%s</li>' %
                     (esc(code), esc(label), esc(explanation),
                      ' <span class="used">%d× verwendet</span>' % cut_counts[code] if cut_counts[code] else '')
                     for code, (label, explanation) in _CUT_MEANING.items())
    return ('<div class="overview" aria-label="Ergebnisübersicht">'
            '<div class="metric %s"><strong>%s</strong><span>Textabdeckung und Ausgabe-Zeitgrenze geprüft; '
            'keine Garantie für gemessene Sprechzeit.</span></div>'
            '<div class="metric"><strong>%d semantisch / %d Fallback-Schnitt%s</strong>'
            '<span>Fallback bedeutet nicht automatisch schlechter Inhalt.</span></div>'
            '<div class="metric %s"><strong>%d Jev-Urteil%s fehlgeschlagen</strong>'
            '<span>Das betrifft die Bewertung, nicht die Gültigkeit der Partition.</span></div></div>'
            '%s<p class="note">Ein <code>status: error</code> bei einem Grenzurteil bedeutet '
            '<strong>nicht, dass die Partition ungültig</strong> ist. Ursachen: %s.</p>'
            '<details open><summary>Was bedeuten die Schnittarten?</summary><ul class="legend">%s</ul></details>' %
            ('ok' if valid else 'warn', 'Partition gültig' if valid else 'Partition ungültig',
             cut_counts['accepted_semantic'], sum(n for k, n in cut_counts.items() if k != 'accepted_semantic'),
             '' if sum(n for k, n in cut_counts.items() if k != 'accepted_semantic') == 1 else 'e',
             'warn' if errors else 'ok', error_count, '' if error_count == 1 else 'e',
             key_hint, error_detail, legend))


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
        quality = unit['quality_status']
        quality_label = {'semantic': 'Semantisch gestützt', 'degraded': 'Fallback beteiligt',
                         'unresolved_search': 'Suche unklar'}.get(quality, quality)
        blocks.append('<section><h2>%s — %s bis %s s</h2><p class="unit-meta">'
                      '<span class="badge">%s</span> · Dauer: %s s · Zeitbasis: <code>%s</code>'
                      '</p>%s<pre>%s</pre>%s</section>' %
                      (esc(unit['id']), esc(start), esc(end), esc(quality_label),
                       esc(round(end - start, 3)), esc(unit['timing_basis']), media, esc(unit['text']),
                       _snu_details(unit, esc)))
    cuts = ''.join('<li><strong>%s</strong> <code>%s</code> bei %s s — %s '
                   '<small>(Zeitbasis: %s)</small></li>' %
                   (esc(_CUT_MEANING.get(c['provenance'], (c['provenance'], ''))[0]),
                    esc(c['provenance']), esc(c['time']), esc(c['reason']), esc(c['timing_basis']))
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
    return ('<!doctype html><html lang="de"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>SNU preview — %s</title><style>'
            'body{font:16px/1.5 system-ui;max-width:980px;margin:auto;padding:1.5rem;color:#243044;background:#f7f9fc}'
            'h1,h2{line-height:1.2}iframe,video{width:100%%;aspect-ratio:16/9;border:0}section,details{background:white;'
            'border:1px solid #dce3ed;border-radius:12px;padding:1rem;margin:1rem 0}section{border-left:5px solid #93a6be}'
            'pre{white-space:pre-wrap;overflow-wrap:anywhere}code{overflow-wrap:anywhere}ol li,ul li{margin:.4rem 0}'
            '.overview{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:.8rem}'
            '.metric{background:white;border:1px solid #dce3ed;border-top:4px solid #637ea2;border-radius:10px;padding:1rem}'
            '.metric.ok{border-top-color:#24866a}.metric.warn{border-top-color:#d29228}'
            '.metric strong,.metric span{display:block}.metric span,.note,.unit-meta{color:#536174;font-size:.92rem}'
            '.hint{background:#fff2d8;border-left:4px solid #d29228;padding:.8rem}.badge,.used{background:#e5ebf4;'
            'border-radius:6px;padding:.15rem .45rem}.legend{padding-left:1.3rem}summary{cursor:pointer;font-weight:600}'
            '.snu-details{background:#f7f9fc}.snu-details dl{margin:.8rem 0 0}.snu-details dl>div{display:grid;'
            'grid-template-columns:minmax(150px,35%%) 1fr;gap:.8rem;padding:.35rem 0;border-top:1px solid #e4e9f0}'
            '.snu-details dt{font-weight:600}.snu-details dd{margin:0;overflow-wrap:anywhere}'
            '@media(max-width:600px){.snu-details dl>div{grid-template-columns:1fr;gap:0}}'
            '</style><h1>SNU preview — %s</h1><p>Abspielzeiten folgen den Ausgabe-Zeitstempeln, '
            'nicht einer gemessenen Sprechzeit. YouTube rundet eingebettete Clips auf ganze Sekunden.</p>%s%s'
            '<h2>SNUs und Clips</h2>%s<h2>Gewählte Schnitte</h2><ol>%s</ol>'
            '<details><summary>Technische Grenzurteile (boundary_scores, %d)</summary><ol>%s</ol></details>'
            '<details><summary>Reproduzierbarkeit: Policy, Normalisierung und Nutzung</summary><pre>%s</pre></details>%s</html>' %
            (esc(data['title']), esc(data['title']), _quality_overview(data, esc),
             '<p class="note">Klassifikation wird in der Vorschau übersprungen; sie ist unabhängig von der Schnittqualität.</p>',
             ''.join(blocks), cuts, len(data['boundary_scores']), scores,
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
    print('Preview: %s (%d SNUs, %d Fallback-Schnitte; Jev %d Requests)' %
          (page.absolute(), result['snus'], result['forced'], result['usage']['requests']), flush=True)
    if result.get('judge_errors'):
        print('Jev-Urteile fehlgeschlagen: %s. Partition trotzdem gültig; Details in preview.html.' %
              ', '.join(result['judge_errors']), flush=True)
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
