"""Run one assessment locally.

    python -m src.agent.run --mock   # offline demo: REAL read-only database data + fixture research + template drafting
    python -m src.agent.run --live   # real Tavily, GDELT and OpenAI calls. REFUSED unless ALLOW_PAID_CALLS=1 is set
                                     # and OPENAI_MODEL is configured. Requires explicit user approval beforehand.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from src.agent.data import PostgresData
from src.agent.graph import Deps, run_assessment
from src.agent.llm import LLMError, OpenAILLM
from src.agent.mock import DemoLLM, MockSearch, build_mock_policy

RUNS = Path(__file__).resolve().parents[2] / "runs"
# USD per million tokens (input, output) from OpenAI's model page, checked 10 Nov 2026; verify before relying on it.
PRICE_PER_M = {"gpt-5.4-mini": (0.75, 4.50)}


def estimate_cost(model: str | None, input_tokens: int, output_tokens: int) -> float | None:
    if model not in PRICE_PER_M:
        return None
    pin, pout = PRICE_PER_M[model]
    return round(input_tokens / 1e6 * pin + output_tokens / 1e6 * pout, 6)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--mock", action="store_true", help="offline demo with fixtures (no external calls)")
    mode.add_argument("--live", action="store_true", help="real paid/quota API calls (needs ALLOW_PAID_CALLS=1)")
    ap.add_argument("--ingredient", default="sunflower_oil")
    ap.add_argument("--horizon", type=int, default=3)
    ap.add_argument("--request-id", default="local-001")
    ap.add_argument("--out", type=Path, default=RUNS)
    ap.add_argument("--max-tokens", type=int, default=40_000, help="hard per-run token cap for the LLM")
    args = ap.parse_args(argv)
    load_dotenv()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")     # Windows consoles (cp1252) cannot print every report character

    if args.live:
        if os.getenv("ALLOW_PAID_CALLS") != "1":
            print("REFUSED: --live makes paid/quota-consuming calls (OpenAI, Tavily). Set ALLOW_PAID_CALLS=1 only after approval.")
            return 3
        from src.tools.search import GdeltTool, TavilyTool
        try:
            llm = OpenAILLM(os.getenv("OPENAI_MODEL"), allow_paid=True, request_id=args.request_id,
                            max_total_tokens=args.max_tokens, reasoning_effort=os.getenv("OPENAI_REASONING_EFFORT") or None)
        except LLMError as exc:
            print(f"REFUSED: {exc.code}")
            return 3
        from src.rag.embed import EmbeddingError, OpenAIEmbedder
        from src.rag.retrieve import PostgresPolicyRetriever
        try:
            embedder = OpenAIEmbedder(allow_paid=True, request_id=args.request_id)
        except EmbeddingError:
            embedder = None                     # policy retrieval then uses the full-text fallback
        deps = Deps(PostgresData(), llm, TavilyTool(os.getenv("TAVILY_API_KEY")), GdeltTool(max_attempts=2),          # at most ONE retry (authorised limit)
                    policy=PostgresPolicyRetriever(embedder), require_research=True)
    else:
        from datetime import date
        today = date.today()
        deps = Deps(PostgresData(), DemoLLM(), MockSearch("tavily", today), MockSearch("gdelt", today), policy=build_mock_policy(), mock=True)

    response, trace = run_assessment({"request_id": args.request_id, "ingredient": args.ingredient,
                                      "horizon_months": args.horizon}, deps)
    args.out.mkdir(parents=True, exist_ok=True)
    cost = estimate_cost(os.getenv("OPENAI_MODEL") if args.live else None, response.execution.tokens["input"],
                         response.execution.tokens["output"])
    (args.out / f"{args.request_id}.json").write_text(json.dumps({"response": response.model_dump(mode="json"), "trace": trace,
                                                                  "model": os.getenv("OPENAI_MODEL") if args.live else "mock",
                                                                  "estimated_cost_usd": cost}, indent=1, default=str), encoding="utf-8")
    if response.report_markdown:
        (args.out / f"{args.request_id}.md").write_text(response.report_markdown, encoding="utf-8")
        print(response.report_markdown)
    print(f"\nSTATUS: {response.status.upper()} | warnings: {len(response.warnings)} | execution: {response.execution.model_dump()}")
    if response.error:
        print(f"ERROR: {response.error.code}: {response.error.message}")
    return 0 if response.status != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
