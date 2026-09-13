#!/usr/bin/env python3
"""Everything the OCR review pane does must reach the saved file.

⚠️⚠️ THE BUG. `tmp_srt` is written ONCE, the instant OCR finishes, before the
review pane can be touched. Save then did `shutil.copy2(tmp_srt.name, dest)`
and Load into Editor re-read the same file. So every applied filter and every
inline edit was discarded — silently, and with a "Saved:" confirmation on
screen. Tony hit it through the filters on 2026-09-13; single inline edits had
been vanishing the same way and looked like they had worked.

The fix is `_flush_cues_to_tmp()`, which rewrites the temp file from the live
cue list (committing any open inline edit first). The failure mode that will
bring this back is not a logic error — it is a NEW consumer of tmp_srt that
forgets to call it. So this test reads the source and checks each consumer.

⚠️ A source-level test is unusual and deliberate here. The consumers live in a
deeply nested closure inside a Tk callback; there is no seam to call them from
a test, and a mock-heavy harness would pin the mocks rather than the code. This
checks the one property that actually matters and fails loudly when a new
save path appears without a flush.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   'modules', 'subtitle_editor.py')

FLUSH = '_flush_cues_to_tmp'


def _ocr_monitor_scope(tree):
    """The function that defines _flush_cues_to_tmp — i.e. the OCR monitor.

    ⚠️ Scoping the search this way is load-bearing. `tmp_srt` is also the name
    of an UNRELATED local in the subtitle-extraction paths (`load_file`,
    `_check_extract`), which convert a stream to SRT via ffmpeg and have no
    review pane at all. A search across the whole module flags those as
    missing the flush, which is a false alarm that would train someone to
    delete this test. The flush helper's own scope is exactly the set of
    functions that share the OCR monitor's tmp_srt.
    """
    candidates = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            if (isinstance(child, ast.FunctionDef) and child.name == FLUSH
                    and child is not node):
                candidates.append(node)
                break
    if not candidates:
        return None
    # ⚠️ INNERMOST, not the first match. Every enclosing function contains the
    # flush helper somewhere in its subtree, and the outermost one is the whole
    # editor — which sweeps in the unrelated extraction-path `tmp_srt` locals
    # and reports them as missing the flush. Smallest span wins.
    return min(candidates,
               key=lambda n: (n.end_lineno or 0) - (n.lineno or 0))


def _functions_using_tmp_srt(tree):
    """Every def in the OCR monitor whose body reads or copies tmp_srt."""
    scope = _ocr_monitor_scope(tree)
    assert scope is not None, f"could not locate the scope defining {FLUSH}"
    out = {}
    for node in ast.walk(scope):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if 'tmp_srt' not in ast.dump(node):
            continue
        reads = False
        for sub in ast.walk(node):
            # shutil.copy2(tmp_srt.name, ...) or open(tmp_srt.name, ...)
            if isinstance(sub, ast.Call):
                fname = ''
                if isinstance(sub.func, ast.Name):
                    fname = sub.func.id
                elif isinstance(sub.func, ast.Attribute):
                    fname = sub.func.attr
                if fname in ('copy2', 'open'):
                    for a in sub.args:
                        if (isinstance(a, ast.Attribute)
                                and isinstance(a.value, ast.Name)
                                and a.value.id == 'tmp_srt'
                                and a.attr == 'name'):
                            reads = True
        if reads:
            out[node.name] = node
    return out


def _calls_flush(node):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            if isinstance(sub.func, ast.Name) and sub.func.id == FLUSH:
                return True
    return False


# ⚠️ Named exemptions, NOT a heuristic. These share the OCR monitor's `tmp_srt`
# but are not part of the OCR flow, so flushing OCR cues into them would clobber
# what they just produced.
#
# I tried to detect this structurally first and could not: _check_extract is a
# SIBLING of the OCR save paths in the same closure, and the ffmpeg command that
# writes its file is built in the ENCLOSING function, so from inside the def it
# looks like a pure read. A heuristic loose enough to exempt it also exempted
# the real consumers. An explicit list that must stay accurate beats a clever
# rule that silently stops catching things.
EXEMPT = {
    '_check_extract':
        'subtitle EXTRACTION path — reads the .srt ffmpeg just wrote for a '
        'non-OCR stream. No review pane is involved and an OCR flush here '
        'would overwrite the extraction.',
}


def test_every_consumer_of_the_temp_srt_flushes_first():
    """⭐ The regression guard. A new Save path that skips the flush produces
    correct-looking output with no error — the most deniable kind of bug."""
    tree = ast.parse(open(SRC, encoding='utf-8').read())
    consumers = _functions_using_tmp_srt(tree)

    # The writer itself and the flush helper are not consumers.
    consumers.pop(FLUSH, None)

    assert consumers, "found no tmp_srt consumers — has the OCR pane moved?"

    # ⚠️ An exemption that no longer matches anything is a rotted exemption —
    # the function was renamed and is now silently unchecked. Fail on that too.
    stale = [n for n in EXEMPT if n not in consumers]
    assert not stale, (
        f"exempted functions no longer read tmp_srt (renamed or removed?) — "
        f"review and update EXEMPT: {stale}")

    missing = [name for name, node in consumers.items()
               if name not in EXEMPT and not _calls_flush(node)]
    assert not missing, (
        f"these read/copy tmp_srt without calling {FLUSH}() first, so the "
        f"review pane's filters and inline edits will not reach the file: "
        f"{sorted(missing)}")


def test_flush_helper_exists_and_commits_the_open_edit():
    """⚠️ An edit still open in the inline overlay has not reached the cue dict
    yet. Flushing without committing it loses the cue being looked at."""
    tree = ast.parse(open(SRC, encoding='utf-8').read())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == FLUSH), None)
    assert fn is not None, f"{FLUSH} is gone"

    calls = [s.func.id for s in ast.walk(fn)
             if isinstance(s, ast.Call) and isinstance(s.func, ast.Name)]
    assert '_end_inline' in calls, (
        "_flush_cues_to_tmp must commit the open inline edit before writing")
    assert 'write_srt_file' in calls, (
        "_flush_cues_to_tmp must rewrite from the live cue list")


def test_write_srt_file_reflects_edits_made_after_ocr():
    """The behaviour the flush relies on: the writer reads the list it is
    given, so a cue edited in place shows up."""
    from modules.subtitle_ocr import write_srt_file
    import tempfile
    cues = [{'index': 1, 'start': '00:00:01,000', 'end': '00:00:02,000',
             'text': 'original', 'img': '/t/1.bmp'}]
    cues[0]['text'] = 'edited by hand'
    path = os.path.join(tempfile.mkdtemp(), 'o.srt')
    write_srt_file(cues, path)
    out = open(path, encoding='utf-8').read()
    assert 'edited by hand' in out
    assert 'original' not in out


if __name__ == '__main__':
    import pytest
    sys.exit(pytest.main([__file__, '-q']))
