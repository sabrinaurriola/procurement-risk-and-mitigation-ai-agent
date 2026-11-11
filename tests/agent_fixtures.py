"""Builders for agent tests: scripted LLM, fake clock, static internal data. No network, no database."""
from __future__ import annotations

from datetime import date

from src.agent.config import Limits
from src.agent.data import InternalData, StaticData, SupplierInfo
from src.agent.graph import Deps
from src.agent.mock import DemoLLM, MockSearch, build_mock_policy
from src.agent.schemas import RefinedQueries
from tests.calc_fixtures import KEY, benchmark_history, contracts, forecasts

TODAY = date(2026, 11, 10)


def suppliers():
    return {"SUP-001": SupplierInfo("SUP-001", "Aceites del Sur S.A. (fictional)", "ES", "approved"),
            "SUP-002": SupplierInfo("SUP-002", "Girasol Iberico S.L. (fictional)", "ES", "approved"),
            "SUP-003": SupplierInfo("SUP-003", "Danube Agri Oils SRL (fictional)", "RO", "conditional")}


def internal(**kw) -> InternalData:
    base = dict(benchmark_key=KEY, contracts=contracts(), forecasts=forecasts(), observations=benchmark_history(),
                suppliers=suppliers())
    base.update(kw)
    return InternalData(**base)


class FakeClock:
    """Monotonic clock that advances by `step` seconds on every read."""

    def __init__(self, step: float = 0.0):
        self.t, self.step = 1000.0, step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


class ScriptedLLM(DemoLLM):
    """DemoLLM with scripted draft outputs and unique refined queries; records calls."""

    def __init__(self, drafts=None, judge_error: Exception | None = None):
        super().__init__()
        self.drafts, self.judge_error = list(drafts or []), judge_error
        self.draft_calls = 0
        self.judged: list[list[dict]] = []
        self.draft_args: list[tuple] = []
        self.refine_n = 0

    def judge_evidence(self, ingredient, items):
        if self.judge_error:
            raise self.judge_error
        self.judged.append(items)
        return super().judge_evidence(ingredient, items)

    def refine_queries(self, ingredient, context):
        self.refine_n += 1
        n = self.refine_n
        return RefinedQueries(tavily_queries=[f"sunflower oil query {n}a", f"sunflower oil query {n}b"],
                              gdelt_query=f'"sunflower oil" round {n}')

    def draft_report(self, ingredient, facts_text, evidence, problems, max_candidates):
        self.draft_calls += 1
        self.draft_args.append((facts_text, evidence, list(problems)))
        if self.drafts:
            nxt = self.drafts.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            if callable(nxt):
                return nxt(super().draft_report(ingredient, facts_text, evidence, problems, max_candidates))
            return nxt
        return super().draft_report(ingredient, facts_text, evidence, problems, max_candidates)


class EmptySearch:
    """Returns only irrelevant results, forcing the research loop to keep going until its budget stops it."""

    def __init__(self, name: str):
        self.name, self.calls, self.queries = name, 0, []

    def search(self, query, *, kind, lookback_days, max_results):
        from src.tools.search import RawResult, ToolResult
        self.calls += 1
        self.queries.append(query)
        return ToolResult(True, [RawResult(self.name, f"https://example.invalid/{self.name}/{self.calls}", "Fuel prices", "fossil fuel news",
                                           TODAY, None, query, kind)], None, 1)


def deps(llm=None, tavily="mock", gdelt="mock", data=None, limits=None, clock=None, today=TODAY, mock=True, policy="mock") -> Deps:
    return Deps(data=data or StaticData(internal()), llm=llm or ScriptedLLM(), policy=build_mock_policy() if policy == "mock" else policy,
                tavily=MockSearch("tavily", today) if tavily == "mock" else tavily,
                gdelt=MockSearch("gdelt", today) if gdelt == "mock" else gdelt,
                limits=limits or Limits(), clock=clock or FakeClock(), today=lambda: today, mock=mock)


REQUEST = {"request_id": "test-001", "ingredient": "sunflower_oil", "horizon_months": 3}
