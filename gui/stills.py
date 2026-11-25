"""
The exact frame at a time, decoded on a thread.

VLC does not redraw when it seeks while paused, so it cannot be used to place
cuts: this shows what is at the playhead, on the same clock as the cutter.
"""

import queue
import threading

import av

from audio_match import playback_origin

MAX_SIDE = 1600


class _Reader:
    def __init__(self, path):
        self.path = path
        self._open()
        self.origin = playback_origin(path)

    def _open(self):
        self.container = av.open(self.path)
        video = [s for s in self.container.streams.video
                 if not s.disposition & av.stream.Disposition.attached_pic]
        self.stream = video[0] if video else None
        self.frames = self.current = self.following = None
        if self.stream:
            self.stream.thread_type = 'AUTO'

    def close(self):
        self.frames = None
        self.container.close()

    def _time(self, frame):
        return float(frame.pts * self.stream.time_base) - self.origin

    def _advance(self):
        for frame in self.frames:
            if frame.pts is not None:
                return frame
        return None

    def _restart(self, seconds):
        offset = int((seconds + self.origin) / self.stream.time_base)
        self.container.seek(max(0, offset), stream=self.stream, backward=True)
        self.frames = self.container.decode(self.stream)
        self.current = self._advance()
        self.following = self._advance()

    def frame_at(self, seconds):
        if not self.stream:
            return None
        seconds += 0.002  # times are stored to the millisecond
        ahead = self.current is not None and 0 <= seconds - self._time(self.current) < 1.5
        if not ahead:
            self._restart(seconds)
            if self.current is None or self._time(self.current) > seconds:
                # Some containers seek late: step back further.
                for back in (3.0, 12.0):
                    self._restart(max(0.0, seconds - back))
                    if self.current is not None and self._time(self.current) <= seconds:
                        break
                else:
                    # MPEG-TS cannot seek to before its second keyframe.
                    self.close()
                    self._open()
                    self.frames = self.container.decode(self.stream)
                    self.current = self._advance()
                    self.following = self._advance()
        while self.following is not None and self._time(self.following) <= seconds:
            self.current, self.following = self.following, self._advance()
        return self.current

    def image_at(self, seconds):
        frame = self.frame_at(seconds)
        if frame is None:
            return None
        aspect = self.stream.sample_aspect_ratio or 1
        width, height = frame.width * float(aspect), float(frame.height)
        scale = min(1.0, MAX_SIDE / max(width, height))
        return frame.reformat(
            width=max(2, int(width * scale) // 2 * 2),
            height=max(2, int(height * scale) // 2 * 2),
            format='rgb24',
        ).to_image()


class StillFrames:
    """Only the newest request is served. Answers arrive in `results`."""

    def __init__(self):
        self.results = queue.Queue()
        self._condition = threading.Condition()
        self._wanted = None
        self._stopped = False
        threading.Thread(target=self._serve, daemon=True).start()

    def request(self, key, path, seconds):
        with self._condition:
            self._wanted = (key, path, seconds)
            self._condition.notify()

    def stop(self):
        with self._condition:
            self._stopped = True
            self._condition.notify()

    def _serve(self):
        reader, opened = None, None
        while True:
            with self._condition:
                while self._wanted is None and not self._stopped:
                    self._condition.wait()
                if self._stopped:
                    break
                (key, path, seconds), self._wanted = self._wanted, None
            try:
                # The key changes on every load, as a file may have been rewritten.
                if opened != (key[0], path):
                    if reader:
                        reader.close()
                    reader, opened = None, None
                    reader = _Reader(path)
                    opened = (key[0], path)
                image = reader.image_at(seconds)
            except Exception as exc:  # a broken file must not end the thread
                self.results.put((key, None, str(exc)))
                opened = None
            else:
                self.results.put((key, image, ''))
        if reader:
            reader.close()
