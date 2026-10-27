"""Opt-in bounded SNU partition; timestamps bound output, not actual speech."""
import math
import json
import os
import re
import shutil
import tempfile
import unicodedata

from . import subtitles


def audit_source(path):
    cues = subtitles.parse(path, raw_cues=True)
    previous = None
    for cue in cues:
        if not all(math.isfinite(t) for t in (cue.start, cue.end)) or cue.end <= cue.start:
            raise ValueError('invalid source cue timing')
        if previous is not None and cue.start < previous:
            raise ValueError('source cue timing overlap or out of order')
        previous = cue.end
    return cues


def _pieces(cues, maximum, margin):
    """Split only overlong cues; prefer sentence, word, then grapheme ends."""
    pieces = []
    for i, cue in enumerate(cues):
        text, duration = cue.text, cue.end - cue.start
        if not text:
            raise ValueError('empty normalized cue')
        if duration <= maximum:
            pieces.append((i, 0, len(text), cue.start, cue.end, False))
            continue
        target = maximum / (1 + margin)
        if target <= 0:
            raise ValueError('impossible estimated cap')
        # Grapheme boundaries: combining marks and joiner sequences travel
        # with their preceding base; no third-party Unicode dependency.
        graphemes = [p for p in range(1, len(text) + 1)
                     if p == len(text) or (not unicodedata.combining(text[p])
                                           and text[p] not in ('\ufe0f', '\u200d')
                                           and not 0x1f3fb <= ord(text[p]) <= 0x1f3ff
                                           and text[p - 1] != '\u200d')]
        sentences = {m.end() for m in re.finditer(r'(?:[。！？；;]+|[.!?]+(?=\s|$))(?:\s+)?', text)}
        words = {m.end() for m in re.finditer(r'\S+\s*', text)}
        start = 0
        while start < len(text):
            limit = min(len(text), int((cue.start + duration * start / len(text) + target - cue.start)
                                       * len(text) / duration + 1e-12))
            feasible = [p for p in graphemes if start < p <= limit]
            if not feasible:
                raise ValueError('impossible estimated cap: single grapheme exceeds cap')
            stop = max((p for p in feasible if p in sentences), default=0) or max(
                (p for p in feasible if p in words), default=0) or feasible[-1]
            a = cue.start + duration * start / len(text)
            b = cue.end if stop == len(text) else cue.start + duration * stop / len(text)
            pieces.append((i, start, stop, a, b, True))
            start = stop
    return pieces


def partition(transcript, verdicts, maximum, margin=.15, timing_basis='source_cue'):
    if not math.isfinite(maximum) or maximum <= 0 or not math.isfinite(margin) or not 0 <= margin < 1:
        raise ValueError('invalid cap or timing margin')
    cues = transcript.cues
    if not cues:
        raise ValueError('no normalized cues')
    pieces = _pieces(cues, maximum, margin)
    by_index = {}
    for v in verdicts:
        if v.cue_index is not None and (v.cue_index not in by_index or
                                        (v.cut and not by_index[v.cue_index].cut) or
                                        (v.cut == by_index[v.cue_index].cut and
                                         (v.good_boundary or 0) > (by_index[v.cue_index].good_boundary or 0))):
            by_index[v.cue_index] = v
    accepted = {v.cue_index for v in verdicts if v.cut and v.cue_index is not None}
    cuts = []
    units = []

    def make_cut(pos, provenance):
        left, right = pieces[pos - 1], pieces[pos]
        verdict = by_index.get(right[0]) if right[1] == 0 else None
        return {'position': [right[0], right[1]], 'provenance': provenance,
                'reason': ('accepted semantic judgment' if provenance == 'accepted_semantic' else
                           'estimated within-cue split' if right[1] else 'hard output-timestamp cap'),
                'timing_basis': 'interpolated_cue' if right[1] else timing_basis,
                'source_timing_basis': timing_basis,
                'judgment': {'id': verdict.cut_id, 'status': verdict.status,
                             'score': verdict.good_boundary, 'reasons': verdict.reasons,
                             'error': verdict.error_code} if verdict else None,
                'time': right[3], 'after': [left[0], left[2]]}

    def emit(lo, hi):
        group = pieces[lo:hi]
        text = '\n'.join(''.join(cues[i].text[a:b] for j, a, b, *_ in group if j == i)
                         for i in dict.fromkeys(p[0] for p in group))
        # Full-cue boundaries retain the historical newline presentation;
        # within-cue fragments retain literal contiguous text.
        if all(not p[5] for p in group):
            text += '\n'
        units.append({'id': 'SNU%d' % (len(units) + 1), 'start': group[0][3],
                      'end': group[-1][4], 'text': text,
                      'source_start': [group[0][0], group[0][1]],
                      'source_stop': [group[-1][0], group[-1][2]],
                      'timing_basis': 'interpolated_cue' if any(p[5] for p in group) else timing_basis,
                      'source_timing_basis': timing_basis,
                      'quality_status': 'degraded' if any(p[5] for p in group) or
                      (units and cuts[-1]['provenance'] != 'accepted_semantic') else 'semantic',
                      'classification_status': 'skipped'})

    lo = 0
    while lo < len(pieces):
        end = next((p for p in range(lo + 1, len(pieces))
                    if pieces[p][1] == 0 and pieces[p][0] in accepted), len(pieces))
        while lo < end:
            if pieces[end - 1][4] - pieces[lo][3] <= maximum:
                emit(lo, end)
                lo = end
                break
            feasible = [p for p in range(lo + 1, end)
                        if pieces[p - 1][4] - pieces[lo][3] <= maximum]
            if not feasible:
                raise ValueError('impossible output-timestamp cap')
            def rank(p):
                right, left = pieces[p], pieces[p - 1]
                v = by_index.get(right[0]) if right[1] == 0 else None
                tier = (4 if v and v.status == 'ok' and not v.cut else
                        3 if right[1] == 0 and right[3] - left[4] >= subtitles.PAUSE else
                        2 if right[1] == 0 and left[2] == len(cues[left[0]].text)
                        and cues[left[0]].text.endswith(subtitles.SENTENCE_END) else
                        1 if right[1] == 0 else 0)
                return (tier, v.good_boundary or 0 if v else 0, p)
            pos = max(feasible, key=rank)
            emit(lo, pos)
            right, left = pieces[pos], pieces[pos - 1]
            v = by_index.get(right[0]) if right[1] == 0 else None
            provenance = ('best_effort_within_cue' if right[1] else
                          'best_effort_judged' if v and v.status == 'ok' else
                          'best_effort_pause' if right[3] - left[4] >= subtitles.PAUSE else
                          'best_effort_sentence' if cues[left[0]].text.endswith(subtitles.SENTENCE_END) else
                          'best_effort_cue')
            cuts.append(make_cut(pos, provenance))
            if provenance != 'accepted_semantic':
                units[-1]['quality_status'] = 'degraded'
            lo = pos
        if lo < len(pieces) and pieces[lo][1] == 0 and pieces[lo][0] in accepted:
            cuts.append(make_cut(lo, 'accepted_semantic'))
    inspected = {v.cue_index for v in verdicts if v.cue_index is not None}
    for unit in units:
        first, last = unit['source_start'][0], unit['source_stop'][0]
        gap = any(i not in inspected for i in range(first + 1, last + 1))
        failed = any(first < (v.cue_index or 0) <= last and
                     (v.status != 'ok' or any('unresolved' in r for r in v.reasons))
                     for v in verdicts)
        if unit['quality_status'] == 'semantic' and (gap or failed):
            unit['quality_status'] = 'unresolved_search'
    validate(transcript, units, cuts, maximum)
    return units, cuts


def validate(transcript, units, cuts, maximum):
    cues = transcript.cues
    if not units or len(cuts) != len(units) - 1:
        raise ValueError('invalid partition count')
    expected = (0, 0)
    previous_end = -math.inf
    for unit in units:
        start = tuple(unit['source_start'])
        stop = tuple(unit['source_stop'])
        if start != expected or not all(math.isfinite(unit[k]) for k in ('start', 'end')):
            raise ValueError('invalid coverage or finite timing')
        fragments = []
        for i in range(start[0], stop[0] + 1):
            lo = start[1] if i == start[0] else 0
            hi = stop[1] if i == stop[0] else len(cues[i].text)
            fragments.append(cues[i].text[lo:hi])
        expected_text = '\n'.join(fragments)
        if start[1] == 0 and stop[1] == len(cues[stop[0]].text):
            expected_text += '\n'
        if unit['text'] != expected_text:
            raise ValueError('invalid normalized text coverage')
        if unit['start'] < previous_end - 1e-12 or unit['end'] <= unit['start'] or unit['end'] - unit['start'] > maximum + 1e-12:
            raise ValueError('invalid order or output-timestamp cap')
        if stop[1] == len(cues[stop[0]].text):
            expected = (stop[0] + 1, 0)
        else:
            expected = stop
        previous_end = unit['end']
    if expected != (len(cues), 0) or any(tuple(c['position']) != tuple(u['source_start']) for c, u in zip(cuts, units[1:])):
        raise ValueError('invalid source coverage or cut position')


def clear_partition_artifacts(out_dir, source):
    """Remove the stable v2 pointer before a failed or skipped run can reuse it."""
    from .store import slug
    folder = os.path.join(out_dir, slug(source) + '-partition')
    if os.path.islink(folder):
        target = os.path.realpath(folder)
        os.unlink(folder)
        if (os.path.dirname(target) == os.path.abspath(out_dir) and
                os.path.basename(target).startswith('.snu-generation-')):
            shutil.rmtree(target)
    elif os.path.isdir(folder):
        # Only migrate a prior generated result; never erase other user files.
        for name in ('snus.json', 'snu-report.md'):
            path = os.path.join(folder, name)
            if os.path.isfile(path) and not os.path.islink(path):
                os.unlink(path)
        os.rmdir(folder)
    return folder


def process_partition(store, client, video, subs, out_dir, title, target, reuse, threshold,
                      min_confidence, require_complete, window, classify, soft_threshold,
                      maximum, search_budget, min_snu_seconds, margin):
    from . import snu
    if not math.isfinite(maximum) or maximum <= 0 or not math.isfinite(margin) or not 0 <= margin < 1:
        raise ValueError('ensure-partition requires finite positive max and timing margin in [0, 1)')
    folder = clear_partition_artifacts(out_dir, video or subs)
    raw = audit_source(subs)
    transcript = store.ingest(subs, video, title=title, target=target, precision=None)
    normalized = transcript.cues
    if any(normalized[i].start < normalized[i - 1].end for i in range(1, len(normalized))):
        raise ValueError('normalized cue timing overlap')
    with open(subs, encoding='utf-8-sig', errors='replace') as fh:
        source = fh.read()
    if not transcript.timed:
        basis = 'estimated_script_reading_rate'
    elif os.path.splitext(subs)[1].lower() in subtitles.SCRIPT_SUFFIXES and not re.search(r'\d\s*-->\s*\d', source):
        basis = 'inferred_start_only_end'
    else:
        basis = 'source_cue'
    before = dict(client.usage)
    verdicts = snu.judge_boundaries(store, client, transcript, reuse=reuse, window=window,
                                    search_budget=search_budget, threshold=threshold,
                                    min_confidence=min_confidence, require_complete=require_complete,
                                    soft_threshold=soft_threshold, min_snu_seconds=min_snu_seconds)
    # The hard cap wins over soft short-tail avoidance: only preserve cuts that
    # were accepted by the directed search, with no forced provenance.
    units, cuts = partition(transcript, verdicts, maximum, margin, basis)
    if classify:
        objects = []
        cue_offsets = snu.cue_segments(transcript)
        for unit in units:
            first, last = unit['source_start'][0], unit['source_stop'][0]
            relevant = [s for s in transcript.segments if s.char_end > cue_offsets[first].char_start
                        and s.char_start < cue_offsets[last].char_end]
            objects.append(snu.SNU(unit['id'], unit['start'], unit['end'], relevant, unit['text']))
        for unit, obj in zip(units, snu.classify_snus(store, client, transcript, objects, reuse=reuse)):
            unit.update(kind=obj.kind, kind_confidence=obj.kind_confidence,
                        standalone=obj.standalone, classification_status='failed' if obj.error_code else 'ok',
                        classification_error=obj.error_code)
    spent = {k: client.usage[k] - before[k] for k in before}
    degraded = sum(c['provenance'] != 'accepted_semantic' for c in cuts)
    affected = [u for u in units if u['quality_status'] != 'semantic']
    total_duration = sum(u['end'] - u['start'] for u in units)
    data = {'schema_version': 2, 'doc_id': transcript.doc_id, 'title': transcript.title,
            'subtitles': transcript.subtitles, 'video': transcript.video,
            'strategy': 'snu-partition', 'rubric': snu.RUBRIC,
            'policy': {'ensure_partition': True, 'max_snu_seconds': maximum,
                       'timing_margin': margin, 'classification': classify},
            'normalization': {'raw_cues': len(raw), 'normalized_cues': len(normalized),
                              'changed_text_cues': sum(a.text != b.text for a, b in zip(raw, normalized)),
                              'dropped_or_merged_cues': len(raw) - len(normalized),
                              'policy': 'subtitles._clean; markup and rolling-caption deduplication'},
            'partition': {'status': 'valid', 'timing_basis': basis,
                          'bound': 'output_timestamps_only_not_measured_speech',
                          'fallback_cuts': degraded, 'accepted_semantic_cuts': len(cuts) - degraded,
                          'affected_units': len(affected),
                          'affected_duration_share': (sum(u['end'] - u['start'] for u in affected) /
                                                      total_duration if total_duration else 0),
                          'short_tails': sum(u['end'] - u['start'] < min_snu_seconds for u in units[1:]),
                          'search_errors': [v.error_code for v in verdicts if v.error_code],
                          'unresolved_reasons': [reason for v in verdicts for reason in v.reasons
                                                 if 'unresolved' in reason]},
            'boundary_scores': [snu.boundary_record(v) for v in verdicts],
            'cuts': cuts, 'snus': units, 'usage': spent}
    encoded = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    validate(transcript, json.loads(encoded)['snus'], json.loads(encoded)['cuts'], maximum)
    report = ('# %s\n\nPartition: valid (output timestamps only; not a measured speech guarantee)\n'
              'Timing basis: %s; interpolated units: %d; fallback cuts: %d\n'
              'Classification: %s\n\n%s\n' %
              (transcript.title, basis, sum(u['timing_basis'] == 'interpolated_cue' for u in units),
               degraded, ', '.join(u['classification_status'] for u in units),
               '\n'.join('%s %s–%s [%s] %s' %
                         (u['id'], u['start'], u['end'], u['quality_status'], u['text']) for u in units)))
    os.makedirs(out_dir, exist_ok=True)
    # Complete immutable generation, then atomically publish a single pointer.
    stage = tempfile.mkdtemp(prefix='.snu-generation-', dir=out_dir)
    link = folder + '.pending'
    try:
        with open(os.path.join(stage, 'snus.json'), 'w', encoding='utf-8') as fh:
            fh.write(encoded)
        with open(os.path.join(stage, 'snu-report.md'), 'w', encoding='utf-8') as fh:
            fh.write(report)
        os.symlink(os.path.basename(stage), link)
        os.replace(link, folder)
    except Exception:
        if os.path.lexists(link):
            os.unlink(link)
        shutil.rmtree(stage)
        raise
    return {'doc_id': transcript.doc_id, 'title': transcript.title, 'folder': folder,
            'candidates': len(transcript.segments), 'boundaries': len(verdicts),
            'cuts': len(cuts), 'snus': len(units), 'unresolved': 0, 'oversized_unresolved': 0,
            'forced': degraded, 'usage': spent, 'reused': sum(v.reused for v in verdicts),
            'errors': [], 'partition_valid': True}