"""Queue keeps moving when the window or one video gets stuck. Run: python tests/test_queue.py"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tabz import app, jobs  # noqa: E402

# A window whose evaluate_js never returns must not block whoever pushes an update.
api = app.Api.__new__(app.Api)
api._outbox = __import__("queue").Queue()
calls = []


class StuckWindow:
    def evaluate_js(self, js):
        calls.append(js)
        threading.Event().wait()


api._window = StuckWindow()
threading.Thread(target=api._send, daemon=True).start()
t = time.time()
for k in range(3):
    api._emit("jobs", [k])
assert time.time() - t < 0.1, "emit waited on the window"
time.sleep(11)
assert len(calls) == 3, f"a stuck push held up the ones after it ({len(calls)} sent)"

# A download that never returns gets dropped after STALL, and the next one starts.
jobs.STALL = 1
started = []


def fake_download(self, job):
    started.append(job.url)
    if job.url == "a":
        threading.Event().wait()        # hangs for good
    job.status = "done"


jobs.Queue._download = fake_download
jobs.DOWNLOADS = 1
q = jobs.Queue(None, dict, lambda *a: None)
a = q.add("a", collection="list", title="a")
b = q.add("b", collection="list", title="b")
time.sleep(3)
assert a.status == "skipped" and "Stopped responding" in a.error, (a.status, a.error)
assert started == ["a", "b"] and b.status == "done", (started, b.status)
print("ok")
