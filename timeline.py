"""The single mapping between source time and output time.

Video, audio, subtitles and chapters all use the same Timeline, so they cannot
drift apart. Times are integer milliseconds (the Matroska timebase), relative
to the start of playback.
"""

from __future__ import annotations

from typing import Iterable, List, Tuple

FOREVER = 10 ** 12
# A cue trimmed by a cut to less than this would only flash on screen.
MIN_TRIMMED_CUE_MS = 100


class Timeline:
    def __init__(self, segments: Iterable[Tuple[float, float]] = ()):
        """segments: (start, end) intervals to remove, in seconds."""
        self.cuts: List[Tuple[int, int]] = []

        for start, end in sorted(segments):
            a, b = max(0, round(start * 1000)), round(end * 1000)
            if b <= a:
                continue
            if self.cuts and a <= self.cuts[-1][1] + 1:
                self.cuts[-1] = (self.cuts[-1][0], max(b, self.cuts[-1][1]))
            else:
                self.cuts.append((a, b))

    def removed(self, t: int) -> bool:
        return any(a <= t < b for a, b in self.cuts)

    def map(self, t: int) -> int:
        """Output time of source time t. Removed times map to the junction."""
        return t - sum(min(t, b) - a for a, b in self.cuts if t > a)

    def keeps(self) -> List[Tuple[int, int]]:
        """Kept [start, end) intervals. The last one is open-ended."""
        keeps, cursor = [], -FOREVER
        for a, b in self.cuts:
            if a > 0:
                keeps.append((cursor, a))
            cursor = b
        keeps.append((cursor, FOREVER))
        return keeps

    def span(self, start: int, end: int) -> Tuple[int, int] | None:
        """Retime a [start, end) cue, or None if nothing readable remains."""
        if end <= start:
            return None if self.removed(start) else (self.map(start), self.map(start))

        new_start, new_end = self.map(start), self.map(end)
        length = new_end - new_start
        if length <= 0 or (length < end - start and length < MIN_TRIMMED_CUE_MS):
            return None
        return new_start, new_end
