"""
Docflix Media Suite — Forced Subtitle Editor

Build a FORCED subtitle track for mixed-language media: a sub shown by default that
covers only the foreign-language dialogue, while the main track carries everything.

WHY THIS IS HUMAN-IN-THE-LOOP, not an automatic scan
────────────────────────────────────────────────────
We built the automatic detector first (whisper_subtitles.transcribe_with_forced) and
tested it on four real documentaries. It works — it found Japanese, Arabic, Portuguese
and Italian interviews that WhisperX had silently dropped. But it also confidently
labelled a *suspenseful music cue* as Norwegian at 87%, and translate-mode then
invented dialogue for it ("very, very, very, very, always very focused").

A detector cannot tell a foreign interview from a film score. Tony can, in about four
seconds. So the detector was demoted: it no longer decides, it only proposes. Tony
audits the proposals on a waveform, drags the edges, deletes the music, marks anything
the scan missed — and ONLY then does translation run.

That inversion changes the tuning target. When the detector's output went straight to
the translator, every false positive became a garbage subtitle, so sensitivity had to
stay low — and low sensitivity is exactly what made it miss ten seconds of Shona at
6:33 in E01. With a human filter, a false positive costs one click, so the detector
should now be run WIDE OPEN. Recall over precision. (Same conclusion we reached about
the Easynews search page the same morning, for the same reason.)

    -- Arthur & Tony, 2026-08-03
"""

import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .subtitle_filters import ms_to_srt_ts, srt_ts_to_ms
from .waveform_timeline import WaveformTimeline

# Status of each candidate span. Everything starts PENDING; nothing is translated
# until Tony has said yes, because that's the whole point of this panel.
PENDING, ACCEPTED, REJECTED = "pending", "accepted", "rejected"

STATUS_ICON = {PENDING: "•", ACCEPTED: "✓", REJECTED: "✗"}


class ForcedSpan:
    """One candidate stretch of foreign speech."""

    __slots__ = ("start_ms", "end_ms", "lang", "conf", "text", "status", "source")

    def __init__(self, start_ms, end_ms, lang="?", conf=0.0, text="",
                 status=PENDING, source="scan"):
        self.start_ms = int(start_ms)
        self.end_ms = int(end_ms)
        self.lang = lang
        self.conf = conf
        self.text = text
        self.status = status
        self.source = source

    @property
    def duration_ms(self):
        return max(0, self.end_ms - self.start_ms)

    def as_cue(self):
        """Shape the timeline expects — it draws any dict with start/end/text."""
        label = self.text or f"{STATUS_ICON[self.status]} {self.lang} {self.conf:.0%}"
        return {"start": ms_to_srt_ts(self.start_ms),
                "end": ms_to_srt_ts(self.end_ms),
                "text": label}


def open_forced_subs_panel(parent, video_path=None, log_fn=None):
    """Open the Forced Subtitle Editor as its own window.

    Deliberately a separate window with its OWN span list and its OWN timeline
    instance. subtitle_editor.py is ~4.7k lines and its cue model is the live
    editing session; a bug in here must not be able to corrupt a subtitle edit
    in progress. Shares the widget, never the state.
    """
    win = tk.Toplevel(parent)
    win.title("Forced Subtitle Editor")
    win.geometry("1280x820")

    spans: list[ForcedSpan] = []
    state = {"video": video_path, "busy": False}
    events: queue.Queue = queue.Queue()

    # ── layout ───────────────────────────────────────────────────────────────
    top = ttk.Frame(win, padding=6)
    top.pack(fill="x")

    ttk.Label(top, text="Video:").pack(side="left")
    video_var = tk.StringVar(value=video_path or "")
    ttk.Entry(top, textvariable=video_var, width=64).pack(side="left", padx=4)

    ttk.Label(top, text="Model:").pack(side="left", padx=(10, 2))
    model_var = tk.StringVar(value="large-v3")
    ttk.Combobox(top, textvariable=model_var, width=10, state="readonly",
                 values=("tiny", "base", "small", "medium", "large-v3")).pack(side="left")

    ttk.Label(top, text="Spoken:").pack(side="left", padx=(10, 2))
    native_var = tk.StringVar(value="en")
    ttk.Entry(top, textvariable=native_var, width=5).pack(side="left")

    status_var = tk.StringVar(
        value="Load a video · play it · set markers A and B around foreign speech · Add.")

    body = ttk.Frame(win)
    body.pack(fill="both", expand=True, padx=6)

    # Left: embedded mpv video. Right: the candidate list.
    video_frame = tk.Frame(body, bg="black", width=640, height=360)
    video_frame.pack(side="left", fill="both", expand=True)
    video_frame.pack_propagate(False)

    right = ttk.Frame(body)
    right.pack(side="left", fill="both", expand=True, padx=(6, 0))

    cols = ("status", "time", "dur", "lang", "conf", "text")
    tree = ttk.Treeview(right, columns=cols, show="headings", height=14)
    for col, head, w in (("status", "", 30), ("time", "Start", 80), ("dur", "Len", 55),
                         ("lang", "Lang", 50), ("conf", "Conf", 55), ("text", "Translation", 320)):
        tree.heading(col, text=head)
        tree.column(col, width=w, anchor="w" if col == "text" else "center")
    tree.pack(fill="both", expand=True)

    def _fmt(ms):
        s, ms_ = divmod(int(ms), 1000)
        m, s = divmod(s, 60)
        return f"{m}:{s:02d}.{ms_ // 100}"

    def refresh():
        sel = tree.selection()
        keep = sel[0] if sel else None
        tree.delete(*tree.get_children())
        for i, sp in enumerate(spans):
            tree.insert("", "end", iid=str(i), values=(
                STATUS_ICON[sp.status], _fmt(sp.start_ms), f"{sp.duration_ms / 1000:.1f}s",
                sp.lang, f"{sp.conf:.0%}" if sp.conf else "—",
                (sp.text or "").replace("\n", " ")[:120]))
        if keep is not None and keep in tree.get_children():
            tree.selection_set(keep)
        n_acc = sum(1 for s in spans if s.status == ACCEPTED)
        counts.set(f"{len(spans)} candidate(s) · {n_acc} accepted")
        timeline.refresh()

    counts = tk.StringVar(value="0 candidates")

    # ── timeline (candidate spans ARE cues — the widget draws/drags them free) ──
    tl_frame = ttk.Frame(win)
    tl_frame.pack(fill="both", expand=True, padx=6, pady=(6, 0))

    def cues_fn():
        return [sp.as_cue() for sp in spans]

    def on_cue_modified(idx, new_start_ms, new_end_ms):
        """Dragging an edge on the waveform is the primary way to fix a boundary.

        The scan's spans start LATE by construction — the cue that flagged them was
        stretched backwards over the foreign speech — so this gets used constantly.
        """
        if 0 <= idx < len(spans):
            spans[idx].start_ms = int(new_start_ms)
            spans[idx].end_ms = int(new_end_ms)
            spans[idx].text = ""          # boundary changed -> old translation is stale
            refresh()

    def on_selection(idx):
        if 0 <= idx < len(spans):
            tree.selection_set(str(idx))
            tree.see(str(idx))

    timeline = WaveformTimeline(
        tl_frame, cues_fn=cues_fn, on_cue_modified=on_cue_modified,
        on_selection_changed=on_selection, video_frame=video_frame,
        log_fn=(lambda m, level="INFO": status_var.set(m)),
    )
    timeline.pack(fill="both", expand=True)

    # ── manual editing of a span's text/language ─────────────────────────────
    def edit_span(_event=None):
        """Type the translation yourself.

        Not a convenience — a necessity. Whisper "supports" 100 languages, but
        support means it has a token, not that it can translate. On 2026-08-03 a
        Congolese-language segment (Basenji country — Lingala or Kikongo; Kikongo
        isn't in the list at all) came back as "I am not a woman, I am a man" over
        and over. Same failure as the music, one layer down: the model has no way
        to say "I don't know this language well enough", so it produces fluent,
        confident nonsense instead.

        When the machine can't do it, the human must be able to. Otherwise the
        tool is only as good as Whisper's worst language.
        """
        idx = _selected()
        if idx is None:
            return
        sp = spans[idx]
        dlg = tk.Toplevel(win)
        dlg.title(f"Edit span  {_fmt(sp.start_ms)} → {_fmt(sp.end_ms)}")
        dlg.transient(win)
        dlg.geometry("560x260")

        row = ttk.Frame(dlg, padding=8)
        row.pack(fill="x")
        ttk.Label(row, text="Language:").pack(side="left")
        lang_v = tk.StringVar(value=sp.lang if sp.lang != "?" else "")
        ttk.Entry(row, textvariable=lang_v, width=8).pack(side="left", padx=(4, 12))
        ttk.Label(row, text="(free text — e.g. Lingala, Kikongo; Whisper's code need "
                            "not apply)", foreground="#888").pack(side="left")

        ttk.Label(dlg, text="Subtitle text:", padding=(8, 4)).pack(anchor="w")
        txt = tk.Text(dlg, height=6, wrap="word")
        txt.pack(fill="both", expand=True, padx=8)
        txt.insert("1.0", sp.text or "")
        txt.focus_set()

        def save():
            sp.text = txt.get("1.0", "end").strip()
            sp.lang = lang_v.get().strip() or "?"
            if sp.text:
                sp.status = ACCEPTED     # typing a translation IS accepting it
            refresh()
            dlg.destroy()
            status_var.set(f"Span {_fmt(sp.start_ms)} edited by hand.")

        br = ttk.Frame(dlg, padding=8)
        br.pack(fill="x")
        ttk.Button(br, text="Save", command=save).pack(side="right")
        ttk.Button(br, text="Cancel", command=dlg.destroy).pack(side="right", padx=6)
        ttk.Button(br, text="Clear machine text",
                   command=lambda: txt.delete("1.0", "end")).pack(side="left")
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.bind("<Control-Return>", lambda e: save())

    tree.bind("<Double-1>", edit_span)

    # ── drag & drop ──────────────────────────────────────────────────────────
    VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".m4v", ".mov", ".webm", ".ts", ".mpg",
                  ".mpeg", ".wmv", ".flv", ".ogv")

    def _parse_drop(raw):
        """Robust drop parser (matches audio_tools / the main app): file:// URIs,
        {brace-wrapped} paths, or space-separated.

        NEVER raises — a raising drop handler crashes the whole app through tkdnd's
        C layer, and Tony's filenames are full of spaces ("Dogs The Untold Story -
        S01E01 - ...") which is exactly the case that needs brace handling.
        """
        paths = []
        try:
            raw = raw or ""
            if "file://" in raw:
                from urllib.parse import unquote, urlparse
                for line in raw.splitlines():
                    line = line.strip()
                    if line.startswith("file://"):
                        d = unquote(urlparse(line).path)
                        if d:
                            paths.append(d)
            else:
                i = 0
                while i < len(raw):
                    if raw[i] == "{":
                        depth, end = 1, i + 1
                        while end < len(raw) and depth > 0:
                            depth += (raw[end] == "{") - (raw[end] == "}")
                            end += 1
                        paths.append(raw[i + 1:end - 1])
                        i = end + 1 if end < len(raw) else end
                    else:
                        end = raw.find(" ", i)
                        if end == -1:
                            paths.append(raw[i:])
                            break
                        paths.append(raw[i:end])
                        i = end + 1
        except Exception:
            pass
        return paths

    def _on_drop(event):
        try:
            dropped = [p for p in _parse_drop(getattr(event, "data", "")) if p]
            vids = [p for p in dropped
                    if os.path.splitext(p)[1].lower() in VIDEO_EXTS and os.path.exists(p)]
            if not vids:
                status_var.set("Dropped item isn't a video file.")
                return
            if len(vids) > 1:
                # One video at a time — this tool is about one file's audio.
                status_var.set(f"Dropped {len(vids)} videos — loading the first.")
            video_var.set(vids[0])
            load_video()
        except Exception:
            pass  # a raising drop handler hard-kills the app — swallow everything

    def _register_dnd():
        try:
            from tkinterdnd2 import DND_FILES
            for widget in (win, video_frame, tree, tl_frame):
                try:
                    widget.drop_target_register(DND_FILES)
                    widget.dnd_bind("<<Drop>>", _on_drop)
                except Exception:
                    pass
            return True
        except Exception:
            return False   # tkinterdnd2 unavailable — buttons still work

    # ── actions ──────────────────────────────────────────────────────────────
    def _selected():
        sel = tree.selection()
        return int(sel[0]) if sel else None

    def browse():
        p = filedialog.askopenfilename(
            title="Choose video",
            filetypes=[("Video", "*.mkv *.mp4 *.avi *.m4v *.mov *.webm"), ("All", "*.*")])
        if p:
            video_var.set(p)
            load_video()

    def load_video():
        path = video_var.get().strip()
        if not path or not os.path.exists(path):
            messagebox.showerror("Not found", "Pick a video file first.", parent=win)
            return
        state["video"] = path
        status_var.set("Loading waveform…")
        timeline.load_audio(path, done_callback=lambda ok: status_var.set(
            "Waveform ready." if ok else "Waveform failed to load."))

    def set_status(new):
        idx = _selected()
        if idx is None:
            return
        spans[idx].status = new
        refresh()

    def _marker_status():
        a, b = timeline._marker_a_ms, timeline._marker_b_ms
        if a is None and b is None:
            return "A/B not set."
        if a is None or b is None:
            got = "A" if a is not None else "B"
            return f"Marker {got} set — now set the other one."
        return (f"A {_fmt(min(a, b))} → B {_fmt(max(a, b))}  "
                f"({abs(b - a) / 1000:.1f}s) — press Add.")

    def mark(which):
        """Set marker A or B at the playhead.

        The widget's own setters return silently when there's no playback position,
        which reads as a dead button. Say something instead.
        """
        if timeline._duration_ms <= 0:
            status_var.set("Load a video first.")
            return
        if timeline._playback_pos_ms is None:
            status_var.set("Play the video (or click the waveform) to place the "
                           "playhead first, then press A/B.")
            return
        if which == "a":
            timeline._set_marker_a_at_cursor()
        else:
            timeline._set_marker_b_at_cursor()
        status_var.set(_marker_status())

    def clear_markers():
        timeline._marker_a_ms = None
        timeline._marker_b_ms = None
        timeline._full_redraw()
        status_var.set("A/B cleared.")

    def add_from_markers():
        """Tony's own idea: highlight the section and tell it what to listen to.

        A/B markers already exist in the timeline, so a hand-marked span is a
        first-class citizen here — it does not depend on the scan finding anything.
        """
        a, b = timeline._marker_a_ms, timeline._marker_b_ms
        if a is None or b is None:
            messagebox.showinfo(
                "Set markers first",
                "Set marker A and marker B on the timeline around the foreign speech, "
                "then press Add.", parent=win)
            return
        spans.append(ForcedSpan(min(a, b), max(a, b), lang="?", conf=0.0,
                                status=ACCEPTED, source="manual"))
        spans.sort(key=lambda s: s.start_ms)
        # Clear the markers so the next span starts from a clean slate — otherwise
        # a stale A silently pairs with a fresh B and makes a span you didn't mean.
        timeline._marker_a_ms = None
        timeline._marker_b_ms = None
        refresh()
        status_var.set(f"Added span {_fmt(min(a, b))} → {_fmt(max(a, b))} "
                       f"({abs(b - a) / 1000:.1f}s), accepted. A/B cleared.")

    def delete_span():
        idx = _selected()
        if idx is not None:
            spans.pop(idx)
            refresh()

    def play_span():
        idx = _selected()
        if idx is None:
            return
        sp = spans[idx]
        # A little lead-in: if the speech starts before the span, the boundary is late
        # and you want to hear that rather than have it cropped out.
        timeline.set_playback_position(max(0, sp.start_ms - 3000))
        timeline._toggle_playback()

    # ── translate + export ───────────────────────────────────────────────────
    def _accepted():
        return [s for s in spans if s.status == ACCEPTED]

    def translate_accepted():
        """Run Whisper translate on ONLY the spans Tony accepted.

        This ordering is the whole design. When the detector fed the translator
        directly, a music cue became invented dialogue ("very, very, very, very,
        always very focused") — Whisper hallucinates words when handed non-speech.
        Gating on human approval makes that impossible: music never reaches here.
        """
        todo = _accepted()
        if not todo:
            messagebox.showinfo("Nothing accepted",
                                "Accept at least one span first.", parent=win)
            return
        if state["busy"]:
            return
        path = state["video"] or video_var.get().strip()
        if not path or not os.path.exists(path):
            messagebox.showerror("No video", "Load a video first.", parent=win)
            return
        state["busy"] = True
        status_var.set(f"Translating {len(todo)} span(s)…")

        def worker():
            try:
                import tempfile
                import subprocess as sp
                from faster_whisper import WhisperModel
                from faster_whisper.audio import decode_audio

                with tempfile.TemporaryDirectory() as tmp:
                    wav = os.path.join(tmp, "a.wav")
                    r = sp.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-vn",
                                "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", wav],
                               capture_output=True, text=True)
                    if r.returncode != 0:
                        raise RuntimeError(r.stderr[-400:])
                    audio = decode_audio(wav, sampling_rate=16000)
                    model = WhisperModel(model_var.get(), device="cuda", compute_type="auto")
                    for n, sp_ in enumerate(todo, 1):
                        events.put(("status", f"Translating {n}/{len(todo)}…"))
                        chunk = audio[int(sp_.start_ms / 1000 * 16000):
                                      int(sp_.end_ms / 1000 * 16000)]
                        if len(chunk) < 16000:
                            continue
                        # Let Whisper pick the source language per span unless the
                        # scan already identified one we trust.
                        lang = sp_.lang if sp_.lang and sp_.lang != "?" else None
                        segs, info = model.transcribe(chunk, task="translate",
                                                      language=lang, vad_filter=False)
                        text = " ".join(s.text.strip() for s in segs).strip()
                        events.put(("text", (sp_, text,
                                             getattr(info, "language", sp_.lang))))
                events.put(("status", "Translation done."))
            except Exception as exc:
                events.put(("status", f"Translate failed: {exc}"))
            finally:
                events.put(("done", None))

        threading.Thread(target=worker, daemon=True).start()

    def export_srt():
        done = [s for s in _accepted() if s.text.strip()]
        if not done:
            messagebox.showinfo("Nothing to export",
                                "Accept spans and translate them first.", parent=win)
            return
        base = os.path.splitext(state["video"] or video_var.get())[0]
        out = filedialog.asksaveasfilename(
            parent=win, defaultextension=".srt", initialfile=os.path.basename(base) + ".forced.srt",
            initialdir=os.path.dirname(base), filetypes=[("SubRip", "*.srt")])
        if not out:
            return
        done.sort(key=lambda s: s.start_ms)
        lines = []
        for i, s in enumerate(done, 1):
            lines += [str(i),
                      f"{ms_to_srt_ts(s.start_ms)} --> {ms_to_srt_ts(s.end_ms)}",
                      s.text.strip(), ""]
        with open(out, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        status_var.set(f"Wrote {len(done)} forced cue(s) → {os.path.basename(out)}")
        messagebox.showinfo(
            "Exported",
            f"{len(done)} forced cue(s) written to:\n{out}\n\n"
            "Use \"Mux into video\" to add it as a properly-flagged forced track.",
            parent=win)

    def _existing_sub_count(path):
        """How many subtitle streams the video already has.

        Needed because the new track's index depends on it — a hardcoded `s:1`
        is only right for a file with exactly one existing subtitle track.
        """
        import subprocess as sp
        try:
            r = sp.run(["ffprobe", "-v", "error", "-select_streams", "s",
                        "-show_entries", "stream=index", "-of", "csv=p=0", path],
                       capture_output=True, text=True, timeout=30)
            return len([x for x in r.stdout.split() if x.strip()])
        except Exception:
            return 0

    def mux_into_video():
        """Add the forced track to a COPY of the video, with correct dispositions.

        Two things this gets right that a hand-written command usually doesn't:

        1. The new track's index is computed from the file, not assumed.
        2. `default` is CLEARED on every pre-existing subtitle track. A forced
           track is meant to appear on its own; if the full English track is also
           default (Tony's WhisperX subs are), a player shows both at once — full
           subtitles plus forced subtitles stacked, which defeats the point.

        Writes to a NEW file. Never edits the original in place.
        """
        import subprocess as sp
        done = [s for s in _accepted() if s.text.strip()]
        video = state["video"] or video_var.get().strip()
        if not done or not video or not os.path.exists(video):
            messagebox.showinfo("Not ready",
                                "Need a loaded video and at least one translated, "
                                "accepted span.", parent=win)
            return
        srt = filedialog.askopenfilename(
            parent=win, title="Choose the .forced.srt you exported",
            initialdir=os.path.dirname(video), filetypes=[("SubRip", "*.srt")])
        if not srt:
            return
        base, ext = os.path.splitext(video)
        out = base + ".forced" + (ext or ".mkv")
        n_existing = _existing_sub_count(video)

        cmd = ["ffmpeg", "-v", "error", "-y", "-i", video, "-i", srt,
               "-map", "0", "-map", "1", "-c", "copy", "-c:s", "srt"]
        for i in range(n_existing):                 # demote the existing tracks
            cmd += [f"-disposition:s:{i}", "0"]
        cmd += [f"-disposition:s:{n_existing}", "forced+default",
                f"-metadata:s:s:{n_existing}", f"language={native_var.get() or 'eng'}",
                f"-metadata:s:s:{n_existing}", "title=Forced", out]

        status_var.set("Muxing…")
        r = sp.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            messagebox.showerror("Mux failed", r.stderr[-800:], parent=win)
            status_var.set("Mux failed.")
            return
        status_var.set(f"Wrote {os.path.basename(out)}")
        messagebox.showinfo(
            "Muxed",
            f"Wrote:\n{out}\n\n"
            f"Existing subtitle track(s): {n_existing} — default cleared on each.\n"
            f"New forced track is s:{n_existing} (forced + default).\n\n"
            "Original file untouched.", parent=win)

    def scan_for_candidates():
        """Optional hint pass. NOT the primary workflow — a button, never automatic.

        Everything it produces arrives as PENDING and must be accepted by ear before
        it can be translated. Measured on real documentaries the same day this was
        written: it finds genuine Japanese/Arabic/Portuguese interviews, and it also
        calls a suspenseful music cue "Norwegian, 87%". It cannot do otherwise —
        detect_language must return one of ~99 languages and has no way to answer
        "that isn't speech". The percentage is a best-fit, not a probability of truth.
        """
        path = state["video"] or video_var.get().strip()
        if not path or not os.path.exists(path):
            messagebox.showerror("No video", "Load a video first.", parent=win)
            return
        if state["busy"]:
            return
        if not messagebox.askokcancel(
                "Scan for candidates",
                "This is a HINT pass, not an authority.\n\n"
                "It will miss quiet or short foreign speech, and it will flag music "
                "as foreign dialogue with high confidence — it has no way to say "
                "\"that isn't speech\".\n\n"
                "Everything it finds arrives unaccepted. Check each one by ear.\n\n"
                "Takes a few minutes. Continue?", parent=win):
            return
        state["busy"] = True
        status_var.set("Scanning… (transcribe + language sweep)")

        def worker():
            try:
                import tempfile
                import subprocess as sp
                from . import whisper_subtitles as ws

                with tempfile.TemporaryDirectory() as tmp:
                    wav = os.path.join(tmp, "a.wav")
                    r = sp.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-vn",
                                "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", wav],
                               capture_output=True, text=True)
                    if r.returncode != 0:
                        raise RuntimeError(r.stderr[-400:])
                    _main, _forced, report = ws.transcribe_with_forced(
                        wav, model_var.get(), None, "cuda", 5, vad=True,
                        native_lang=native_var.get().strip() or "en",
                        progress=lambda m: events.put(("status", m)))
                found = [ForcedSpan(s["start"] * 1000, s["end"] * 1000,
                                    s["lang"], s["prob"], status=PENDING, source="scan")
                         for s in report.get("spans", [])]
                events.put(("spans", found))
                events.put(("status",
                            f"Scan found {len(found)} candidate(s) — none accepted yet. "
                            "Play each one before trusting it."))
            except Exception as exc:
                events.put(("status", f"Scan failed: {exc}"))
            finally:
                events.put(("done", None))

        threading.Thread(target=worker, daemon=True).start()

    def pump():
        """Drain worker events on the Tk thread."""
        try:
            while True:
                kind, payload = events.get_nowait()
                if kind == "status":
                    status_var.set(payload)
                elif kind == "text":
                    span_obj, text, lang = payload
                    span_obj.text = text
                    if lang and span_obj.lang in ("?", "", None):
                        span_obj.lang = lang
                    refresh()
                elif kind == "spans":
                    spans.extend(payload)
                    spans.sort(key=lambda s: s.start_ms)
                    refresh()
                elif kind == "done":
                    state["busy"] = False
        except queue.Empty:
            pass
        win.after(150, pump)

    if _register_dnd():
        status_var.set("Drop a video anywhere on this window — or Browse. "
                       "Then: play · A · B · Enter.")

    win.after(150, pump)

    # Keyboard: the widget's docstrings promised "(A key)"/"(B key)" but nothing
    # was ever bound. Marking by ear should not require aiming at a button.
    #
    # Guarded on focus: these are bound to the WINDOW, so without this check,
    # typing "a" in the Spoken-language or Video-path entry would silently drop a
    # marker instead of a letter.
    def _typing():
        w = win.focus_get()
        return isinstance(w, (tk.Entry, ttk.Entry, ttk.Combobox))

    def _key(fn):
        def handler(event):
            if _typing():
                return None
            fn()
            return "break"
        return handler

    for seq, fn in (("<a>", lambda: mark("a")), ("<A>", lambda: mark("a")),
                    ("<b>", lambda: mark("b")), ("<B>", lambda: mark("b")),
                    ("<Return>", add_from_markers),
                    ("<Escape>", clear_markers)):
        win.bind(seq, _key(fn))

    btns = ttk.Frame(win, padding=6)
    btns.pack(fill="x")
    ttk.Button(btns, text="Browse…", command=browse).pack(side="left")
    ttk.Button(btns, text="Load", command=load_video).pack(side="left", padx=3)
    ttk.Button(btns, text="Scan (hint)", command=scan_for_candidates).pack(side="left", padx=3)
    ttk.Separator(btns, orient="vertical").pack(side="left", fill="y", padx=8)
    ttk.Button(btns, text="▶ Play span", command=play_span).pack(side="left", padx=3)
    ttk.Separator(btns, orient="vertical").pack(side="left", fill="y", padx=8)
    ttk.Button(btns, text="Mark A", command=lambda: mark("a")).pack(side="left", padx=2)
    ttk.Button(btns, text="Mark B", command=lambda: mark("b")).pack(side="left", padx=2)
    ttk.Button(btns, text="Clear A/B", command=clear_markers).pack(side="left", padx=2)
    ttk.Button(btns, text="✓ Accept", command=lambda: set_status(ACCEPTED)).pack(side="left", padx=3)
    ttk.Button(btns, text="✗ Reject", command=lambda: set_status(REJECTED)).pack(side="left", padx=3)
    ttk.Button(btns, text="+ Add from A–B", command=add_from_markers).pack(side="left", padx=3)
    ttk.Button(btns, text="Edit text…", command=edit_span).pack(side="left", padx=3)
    ttk.Button(btns, text="Delete", command=delete_span).pack(side="left", padx=3)
    ttk.Separator(btns, orient="vertical").pack(side="left", fill="y", padx=8)
    ttk.Button(btns, text="Translate accepted",
               command=translate_accepted).pack(side="left", padx=3)
    ttk.Button(btns, text="Export .forced.srt",
               command=export_srt).pack(side="left", padx=3)
    ttk.Button(btns, text="Mux into video",
               command=mux_into_video).pack(side="left", padx=3)
    ttk.Label(btns, textvariable=counts).pack(side="right")

    bar = ttk.Frame(win, padding=(6, 0, 6, 6))
    bar.pack(fill="x")
    ttk.Label(bar, textvariable=status_var, foreground="#888").pack(side="left")

    win.forced_spans = spans        # exposed for tests / the scan+translate wiring
    win.forced_refresh = refresh
    win.forced_events = events
    win.forced_state = state
    win.forced_timeline = timeline
    win.forced_vars = {"model": model_var, "native": native_var, "video": video_var,
                       "status": status_var}
    win.forced_scan = scan_for_candidates
    win.forced_parse_drop = _parse_drop   # exposed so the drop path is testable
    win.forced_on_drop = _on_drop
    return win
