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
    # With "Not with the current samples": the search settings when it was sent
    # back. It is only searched again once there is a new sample or setting.
    rescan_after: Optional[tuple] = None
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

    def waiting(self, settings) -> bool:
        """Sent back to wait for new samples, and none has been added since."""
        if not (self.needs_rescan and self.rescan_after) or settings is None:
            return False
        samples, *others = settings
        old_samples, *old_others = self.rescan_after
        return set(samples) <= set(old_samples) and others == old_others

    def to_search(self, settings) -> bool:
        """Whether "Find matches" searches this video with these settings."""
        if self.reviewed or self.waiting(settings):
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
            return 'Waits for new samples' if self.rescan_after else 'Needs re-scan'
        return cuts if count else 'No cuts'
