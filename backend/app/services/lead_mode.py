"""Does a board lead delegate, or does it do the work itself? (ADR-089)

The lead instructions in SOUL.md, the Operating Card and the dispatch message
come from the fleet model: the lead writes no code and hands every job to a
team. That only makes sense when there IS a team. A lead that is the only
agent on its board — every fresh install after the first run — would be told
to delegate to nobody (the dispatch literally said "No team agents found").

One rule, read by every surface that tells an agent how to work, so the three
can never disagree:

* a board lead with at least one other agent on its board delegates (unchanged);
* a board lead alone on its board does the work itself and asks the operator
  before an irreversible step (``mc question``).
"""
from __future__ import annotations


def lead_works_itself(*, is_lead: bool, teammates: int) -> bool:
    """True when a board lead has nobody on its board to delegate to."""
    return bool(is_lead) and teammates <= 0
