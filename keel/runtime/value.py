"""The return ledger.

Every committed run records the minutes of human work it displaced, using a
per-tier estimate registered up front rather than a number invented at
runtime. That matters for the argument: a team that disputes the figure is
disputing a registered constant attached to their agent, and the answer is
the query rather than an opinion about how useful the thing feels.

The estimate is deliberately conservative and deliberately visible. Better to
under-claim a real number than publish an impressive one nobody trusts.
"""

from __future__ import annotations

from ..registry.models import RiskTier

#: Minutes of human work one run of an agent at this tier displaces. Set with
#: the team that owns the workflow, reviewed quarterly, versioned here.
BASELINE_MINUTES = {
    RiskTier.T0_READ_INTERNAL: 6.0,     # a lookup someone would have done by hand
    RiskTier.T1_DRAFT_INTERNAL: 18.0,   # a first draft of a recurring document
    RiskTier.T2_SYSTEM_OF_RECORD: 12.0, # transcription and filing
    RiskTier.T3_EXTERNAL_FACING: 9.0,   # a message composed and sent
}


def minutes_for(tier: RiskTier | int, turns: int) -> float:
    tier = RiskTier(int(tier))
    return round(BASELINE_MINUTES[tier] + 0.5 * turns, 2)
