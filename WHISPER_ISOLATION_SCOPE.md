# Scoping: move the Whisper backends into an isolated venv

> Written 2026-08-18 after installing whisperx from the Suite downgraded the shared
> `~/.local` site-packages and took **Merlin's speech-to-text** offline. Root cause was a
> `transformers<4.45` pin (correct when written, obsolete since) that made pip backtrack to
> whisperx 3.3.1 → `ctranslate2<4.5.0` → a cuDNN 8 build on a cuDNN 9 box. The pins are fixed;
> this document is about the *class* of failure, not that instance.
>
> **The property we want:** the Suite must never be able to change a package another program
> on the machine depends on. Pinning makes that rarer. Only isolation makes it impossible.

## Why the probe alone isn't enough

A post-install smoke test answers *"did whisper come out working?"* It cannot answer *"did I
just break something else on this machine?"* — that damage is invisible from inside the Suite.
Today the collateral was Merlin, and it was caught only because Tony guessed the connection.
Worth building, but it is the second line of defence, not the fix.

## The good news: the boundary is JSON-shaped

Everything that needs to cross the process boundary is plain data:

- **in** — media path, model size, language, task, beam size, vad flags, device, device_index,
  batch_size, word_timestamps, clip_timestamps
- **out** — segments (`start`, `end`, `text`, `avg_logprob`, `words[]`), detected language,
  progress, errors

No tensors, no model handles, no callables. That is what makes this tractable at all.

## The pattern already exists in this codebase

`modules/torch_upscaler.py` solved exactly this problem for torch:

| piece | where |
|---|---|
| `VENV_DIR = ~/.local/share/docflix/torch-engine/venv` | `torch_upscaler.py:34` |
| `find_torch_python()` — locate, or build with `python -m venv` | `:97`, `:208` |
| install into the venv, stream pip output to the log | `:222` |
| **`_probe_python()` — verify CUDA actually works before declaring ready** | `:236` |
| job file + `subprocess.Popen([py, worker, job_file])` | `:409`, `:580` |
| line-delimited JSON on stdout → `json.loads(line)` for progress | `:426`, `:591` |

Roughly 60% of the plumbing is already written and proven. This is applying Tony's own
solved design to the one module that never got it — not inventing anything.

## What has to change

### 9 model-loading entry points

| function | file:line | backend |
|---|---|---|
| `transcribe()` | `whisper_subtitles.py:588` | faster-whisper |
| `transcribe_with_forced()` | `whisper_subtitles.py:897` | faster-whisper |
| `transcribe_whisperx()` | `whisper_subtitles.py:1022` | whisperx |
| `smart_sync()` | `smart_sync.py:52,74` | both |
| `smart_sync()` | `video_converter.py:2000,2018` | both — **see duplication below** |
| `worker()` | `forced_subs_panel.py:447` | faster-whisper |
| `_run_whisperx()` | `whisper_transcriber.py:283` | whisperx |
| `_run_faster_whisper()` | `whisper_transcriber.py:393` | faster-whisper |
| `_show_smart_sync()` | `subtitle_editor.py:4186` | faster-whisper |

They collapse to **4 worker operations**: `probe`, `transcribe`, `detect_language`, `align`.
Most call sites above already funnel through these functions, so the callers largely don't move.

### Availability checks — trivial

`check_dependencies()` and `is_backend_available()` (`whisper_subtitles.py:31-58`) currently do
a bare `import`. They become "does the venv python exist and does it import" — mirroring
`find_torch_python()`.

### ⚠️ The hard part: `transcribe_with_forced()` is iterative, not one-shot

The forced-subtitle work is chatty by design — find suspect windows by `avg_logprob`, run
`detect_language()` on *those* windows only, re-transcribe clips with `clip_timestamps`. Naively
proxying each call across the boundary means many round trips and a model reloaded or held per
call.

**The algorithm must move INTO the worker**, not be driven from outside it. That is the single
biggest piece of real work here, and the piece most likely to change behaviour if done carelessly.
⚠️ Its thresholds were tuned **by ear** on faster-whisper 1.2.1 — see the forced-subtitle notes.
Any port must be validated against Tony's three known test cases, not unit tests.

### ⚠️ Pre-work: `smart_sync()` exists TWICE

`modules/smart_sync.py` and `video_converter.py` both define a 400-line `smart_sync()`.
**93% similar — a fork that has drifted, not a copy.** Both carried the obsolete
`transformers<4.45` pin; both had to be fixed separately today.

Converting whisper twice is wasted work, and leaving a drifted fork means a future fix lands in
one copy again. **Dedupe first**, or explicitly decide which one is dead and delete it.

## Open decision: one venv or two?

**Reuse the upscaler's** `torch-engine/venv` — whisperx needs torch anyway, so this costs roughly
one extra GB instead of four, and there's one runtime to build and maintain.
⚠️ But it couples them: if whisperx ever demands a torch the upscaler dislikes, **both** break,
and we are back to resolving conflicts — the exact problem being escaped, just relocated.

**A separate `whisper-engine/venv`** is fully independent and cannot be broken by the upscaler.
Costs a second multi-GB torch on disk.

Recommendation: **separate**. Disk is the cheap resource here; the whole point is independence,
and sharing reintroduces a resolver.

## Also worth fixing while in here

- **CPU-only users download the entire CUDA stack.** A clean resolution pulls 116 packages
  including `nvidia-cudnn-cu12` and friends. `detect_device()` correctly falls back to CPU at
  runtime, but pip already fetched several GB that will never execute. A CPU install path should
  use the PyTorch CPU index.
- **`--break-system-packages` disappears from the Suite entirely** once this lands. That flag
  exists because the distro is explicitly asking software not to do this.

## Honest estimate

This is a project, not an afternoon: a worker script (~350 lines), a client shim (~200), the
smart_sync dedupe (a drifted 400-line fork), porting the forced-subtitle algorithm across the
boundary, and revalidating that algorithm by ear. Call it several sessions.

**Cheap interim step if the full job waits:** the post-install probe. It doesn't prevent
collateral damage, but it turns "silently broken until first use" into "broken, here's why,
roll back?" — which is most of today's ninety minutes.
