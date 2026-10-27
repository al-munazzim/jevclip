# CLI: preview clipped SNUs in a browser

`preview` runs jevclip's opt-in bounded SNU partition, then generates a standalone HTML page. It does **not** download or transcode the video. Each SNU has its own clipped `youtube-nocookie.com` embed (or an HTML5 player for a local file), text, quality/timing labels, and the page lists every boundary verdict and cut provenance.

## Setup

From the repository root (Python 3.9+):

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
# Only for YouTube input; yt-dlp is an optional executable, not a jevclip dependency:
.venv/bin/python -m pip install yt-dlp
export TYPESAFE_API_KEY='YOUR_TYPESAFE_KEY'
```

Make sure `.venv/bin` is on your PATH (`export PATH="$PWD/.venv/bin:$PATH"`); verify with `jevclip --help` and `yt-dlp --version`. You can omit yt-dlp for local videos. Jev receives subtitle text, not video bytes. Missing API key or a failed boundary judgment is recorded as an explicit unresolved/fallback decision, **not** semantic success.

## One command

```bash
jevclip preview https://youtu.be/Vp4LcxHoojI --max-snu-seconds 90
# Optional policy and write-only mode:
jevclip preview https://youtu.be/Vp4LcxHoojI --max-snu-seconds 90 --threshold 0.6 --no-serve
# For a video with English rather than German captions:
jevclip preview https://youtu.be/Vp4LcxHoojI --lang en --max-snu-seconds 90
```

The command fetches German VTT captions by default using yt-dlp (`--skip-download`); `--lang en` selects English instead. It caches each language separately under `jevclip-preview-out/.captions/`, reuses cached Jev judgments in `~/.jevclip/cache.db` (override via `--db` *before* `preview` or `JEVCLIP_DB`), and publishes `jevclip-preview-out/<caption-id>-partition/preview.html` alongside `snus.json` and `snu-report.md` through a single generation pointer. Rerunning with the same output directory reuses downloaded captions; changing the threshold reruns partition policy but can reuse compatible judgments. `--out DIR` changes the output/cache directory. The CLI prints the exact page path.

Without `--no-serve`, it opens a browser at a free `127.0.0.1` port and serves **only** the page; press Ctrl-C to stop. YouTube playback requires network access and the video must allow embeds. Opening the HTML directly as `file://` may be blocked by YouTube; use the default loopback mode. `--no-serve` writes the page and exits without starting a server or opening a browser; the file remains inspectable offline, but embedded YouTube playback still needs the network. No separate app is required.

## Local clipped video

```bash
jevclip preview ./interview.mp4 --subs ./interview.srt --max-snu-seconds 90
jevclip preview ./interview.mp4 --subs ./interview.vtt --no-serve
```

Local input requires a real file and timed SRT/VTT/JSON captions. Each HTML5 player seeks to its SNU start and pauses at its end. In loopback mode, the server also serves **only that chosen video** at `/video` (including byte-range responses); the file URL in `--no-serve` mode is intended for opening the generated HTML locally. Browser seek precision depends on the media/keyframes and is not a frame-accuracy guarantee.

## Inspecting and troubleshooting

- `snus.json` is schema v2: `snus` contain source offsets, output timestamps and timing/quality status; `cuts` identify accepted semantic versus best-effort provenance; `boundary_scores` includes all verdicts; `partition` and `policy` describe the reproducible decisions. The page displays these plus per-run usage. The bound is **output timestamps only**, not empirically measured speech or playback duration. YouTube embeds take integer seconds, so fractional boundaries are rounded outward for the player.
- `yt-dlp executable not found`: install yt-dlp into your PATH; it is only needed when captions are not already cached.
- `yt-dlp could not fetch de VTT captions`: check that the video has German manual or auto captions, is public, and yt-dlp is up to date. Choose another available language with `--lang en` (or another language code). No video is downloaded.
- No video in browser: check network, browser embed restrictions, ad blockers, or region limitations. The generated `snus.json` and HTML remain available.
- `preview requires timed captions`: an untimed `.txt`/`.md` script has inferred reading times, unsuitable for direct clip playback.
- No browser opens: copy the printed `http://127.0.0.1:PORT/` URL into a browser while the command is running. For remote/SSH machines use `--no-serve` and inspect the artifact locally; the server deliberately binds loopback only.
- Bad/missing TypeSafe key: inspect `partition.search_errors`, fallback cuts and per-unit quality. Set `TYPESAFE_API_KEY` and rerun to retry failed judgments; successful cached judgments are reused.

Offline checks: `python -m unittest discover -s tests -p test_preview.py -v` uses a fake yt-dlp and judge; `python -m unittest discover -s tests` runs the full suite. Neither check requires the external API or YouTube.
