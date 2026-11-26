"""What the panels share. Only the Tk thread changes it."""

import os
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from pipeline import Cut

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAVED = 'saved review'  # scanned_with of a video whose cuts come from reviews.json


@dataclass
class Episode:
    path: str
    subfolder: str
    duration: float
    subtitles: int = 0
    cuts: List[Cut] = field(default_factory=list)
    scanned_with: Optional[tuple] = None  # settings of the last scan
    included: bool = True
    reviewed: bool = False  # ticked off in the review: kept, never searched again
    needs_rescan: bool = False  # sent back from the review for a new search
    # With "Keep selected samples": the samples this video is never searched
    # with again. Their ticked cuts are kept; only newer samples are searched.
    skip_samples: Optional[frozenset] = None
    # Every place each sample was heard, from the last search: the review can
    # move a cut to its sample's next best match.
    candidates: List[Cut] = field(default_factory=list)
    status: str = 'Not scanned'
    error: str = ''
    output: str = ''
    junctions: List[float] = field(default_factory=list)  # cut points in the output

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def segments(self) -> List[Tuple[float, float]]:
        return [(cut.start, cut.end) for cut in self.cuts if cut.enabled]

    @property
    def ready(self) -> bool:
        """Reviewed or cut by hand. Others must never be exported as 'clean'."""
        return self.scanned_with is not None or bool(self.segments)

    def search_samples(self, settings) -> list:
        """The ticked samples this video is searched with (not the skipped ones)."""
        skip = self.skip_samples or ()
        return [path for path, _ in settings[0] if path not in skip] if settings else []

    def kept(self, cut) -> bool:
        """A match kept with "Keep selected samples": a new search leaves it alone."""
        return bool(cut.sample and self.skip_samples and cut.sample in self.skip_samples)

    def waiting(self, settings) -> bool:
        """Sent back keeping its samples, and no new sample has been added since."""
        return bool(self.needs_rescan and self.skip_samples and settings
                    and not self.search_samples(settings))

    def to_search(self, settings) -> bool:
        """Whether "Find matches" searches this video with these settings."""
        if self.reviewed or (self.skip_samples and not self.search_samples(settings)):
            return False
        # Cuts restored from reviews.json are kept: no new search for them.
        return self.needs_rescan or self.scanned_with is None or (
            self.scanned_with not in (settings, SAVED))

    def describe(self) -> str:
        """The status shown once the video is reviewed or sent back."""
        count = len(self.segments)
        cuts = f'{count} cut{"s" * (count != 1)}'
        if self.reviewed:
            return f'Reviewed, {cuts}'
        if self.needs_rescan:
            return 'Waits for new samples' if self.skip_samples else 'Needs re-scan'
        return cuts if count else 'No cuts'

    def alternatives(self, cut) -> List[Cut]:
        """Where else this cut's sample was heard, best first (the cut's own place included)."""
        if not cut.sample:
            return []
        others = [c for c in self.cuts if c is not cut and c.sample == cut.sample]
        places = [c for c in self.candidates if c.sample == cut.sample
                  and not any(abs(c.start - o.start) < 0.5 for o in others)]
        # Equal scores (to 0.1 %): the earlier place first.
        return sorted(places, key=lambda c: (-round(c.waveform, 3), c.start))
