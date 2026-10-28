"""Clear Finished during a live batch (Tony, 2026-10-28).

The batch remembered files by ROW POSITION. Clearing finished rows above the
running ones slid every position: progress and the final ✅ landed on other,
never-encoded files, and _batch_next skipped that many waiting files outright.
"""
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import video_converter as vc  # noqa: E402

App = next(c for c in vars(vc).values()
           if isinstance(c, type) and hasattr(c, 'clear_finished'))


class _Tree:
    def __init__(self, rows):
        self.rows = [[f"id{k}", list(v)] for k, v in enumerate(rows)]

    def get_children(self):
        return tuple(r[0] for r in self.rows)

    def item(self, iid, option=None, values=None):
        r = next(r for r in self.rows if r[0] == iid)
        if values is not None:
            r[1] = list(values)
            return None
        return tuple(r[1]) if option == 'values' else {'values': tuple(r[1])}

    def delete(self, iid):
        self.rows = [r for r in self.rows if r[0] != iid]


class _Root:
    def after(self, _ms, fn):
        fn()


def _app(converting=True):
    a = App.__new__(App)
    a.root, a.add_log = _Root(), (lambda *_a, **_k: None)
    names = list("ABCDEFGH")
    stat = ['✅ Done', '✅ Done', '⏳ 40%', '⏳ 10%'] + ['Waiting'] * 4
    a.files = [{'name': n, 'status': s} for n, s in zip(names, stat)]
    a.file_tree = _Tree([[n, '', '', '', s] for n, s in zip(names, stat)])
    a.is_converting, a._batch_lock, a._batch_next = converting, threading.Lock(), 4
    return a


def _statuses(a):
    return [r[1][4] for r in a.file_tree.rows]


def test_running_lanes_keep_their_own_rows_and_nothing_is_skipped():
    a = _app()
    c, d = a.files[2], a.files[3]
    a.clear_finished()
    assert a.files[a._batch_next]['name'] == 'E'          # not skipped
    a.update_file_status(a._row_of(c, 2), '⏳ 80%')
    a.update_file_status(a._row_of(d, 3), '✅ Done')
    assert _statuses(a) == ['⏳ 80%', '✅ Done'] + ['Waiting'] * 4


def test_lane_whose_row_was_cleared_writes_nowhere():
    a = _app()
    a.clear_finished()
    a.update_file_status(a._row_of({'name': 'gone'}, 0), '✅ Done')
    assert _statuses(a)[2:] == ['Waiting'] * 4


def test_idle_clear_leaves_pointer_alone():
    a = _app(converting=False)
    a.clear_finished()
    assert len(a.files) == 6 and a._batch_next == 4
