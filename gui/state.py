"""What the panels share. Only the Tk thread changes it."""

import os
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from pipeline import Cut

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
