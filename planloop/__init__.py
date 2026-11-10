"""planloop - acceptance-centric plan-graph agent runtime.

The pipeline is deliberately explicit:

    spec -> probe -> decompose -> compile -> execute -> verify -> replan

Every stage exchanges plain JSON-serialisable data so runs can be logged,
replayed, diffed and benchmarked.  The design bet is that *verifiability*
(not planner cleverness) is what makes long agent runs reliable.
"""

from planloop.spec import Budget, Criterion, Limits, ProblemSpec, SpecError
from planloop.scheduler import RunResult, run

__all__ = [
    "Budget",
    "Criterion",
    "Limits",
    "ProblemSpec",
    "SpecError",
    "RunResult",
    "run",
]

__version__ = "0.1.0"
