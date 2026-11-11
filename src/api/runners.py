"""Assessment runners. A runner takes the agent's request dict and returns (AssessmentResponse, trace).

S1 wires MOCK mode only: fixture research and template drafting over REAL, READ-ONLY database data (same as `python -m src.agent.run --mock`).
Live mode is deliberately not wired (it needs the paid-call approval of S6)."""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

from src.agent.schemas import AssessmentResponse

Runner = Callable[[dict[str, Any]], tuple[AssessmentResponse, list]]
RunnerFactory = Callable[[str], Runner]


class LiveNotAvailable(Exception):
    """Live mode is not wired in this build."""


def mock_runner(request: dict[str, Any]) -> tuple[AssessmentResponse, list]:
    from src.agent.data import PostgresData
    from src.agent.graph import Deps, run_assessment
    from src.agent.mock import DemoLLM, MockSearch, build_mock_policy

    today = date.today()
    deps = Deps(PostgresData(), DemoLLM(), MockSearch("tavily", today), MockSearch("gdelt", today), policy=build_mock_policy(), mock=True)
    return run_assessment(request, deps)


def default_runner_factory(mode: str) -> Runner:
    if mode == "mock":
        return mock_runner
    raise LiveNotAvailable(mode)
