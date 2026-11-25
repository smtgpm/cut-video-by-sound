"""Background work. Workers never touch Tk: they only put events on a queue."""

import queue
import threading
from concurrent.futures import ThreadPoolExecutor

from remove_segments import Cancelled


class Tasks:
    def __init__(self, root, on_change):
        self.root, self.on_change = root, on_change
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self.busy = False
        self.label, self.fraction = '', 0.0
        self._poll()

    def start(self, label, items, work, workers, on_item, on_done, on_log=None):
        """
        work(item, progress, log, cancelled) runs on a thread. The callbacks run
        on the Tk thread: on_item(item, result, error), on_done(was_cancelled).
        """
        if self.busy:
            return False
        self.busy, self.label, self.fraction = True, label, 0.0
        self.cancel_event.clear()
        self._on_item, self._on_done, self._on_log = on_item, on_done, on_log
        self._parts = [0.0] * len(items)

        def one(index, item):
            result, error = None, None
            try:
                if self.cancel_event.is_set():
                    raise Cancelled()
                result = work(
                    item,
                    lambda value: self.events.put(('progress', index, value)),
                    lambda text: self.events.put(('log', index, text)),
                    self.cancel_event.is_set,
                )
            except Exception as exc:
                error = exc
            self.events.put(('item', index, (item, result, error)))

        def everything():
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                for index, item in enumerate(items):
                    pool.submit(one, index, item)
            self.events.put(('done', 0, None))

        threading.Thread(target=everything, daemon=True).start()
        self.on_change()
        return True

    def cancel(self):
        self.cancel_event.set()
        self.label = 'Cancelling…'
        self.on_change()

    def _poll(self):
        # Scheduled first, so an error in a callback cannot stop the polling.
        self.root.after(80, self._poll)
        changed = False
        while not self.events.empty():
            kind, index, value = self.events.get()
            changed = True
            if kind == 'progress':
                self._parts[index] = value
            elif kind == 'log':
                if self._on_log:
                    self._on_log(value)
            elif kind == 'item':
                self._parts[index] = 1.0
                self._on_item(*value)
            elif kind == 'done':
                self.busy = False
                self._on_done(self.cancel_event.is_set())
        if changed:
            self.fraction = sum(self._parts) / max(1, len(self._parts))
            self.on_change()
