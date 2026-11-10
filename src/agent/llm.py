"""LLM access. The ONLY place that can spend OpenAI quota, and it is double-guarded:

* constructing OpenAILLM requires allow_paid=True (the CLI sets it only with --live AND ALLOW_PAID_CALLS=1);
* a per-run token cap stops further calls once reached;
* retries are owned by the OpenAI SDK (max_retries=2); there is no second retry layer around it.

Usage is appended (without prompt/response content) to runs/usage.jsonl.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Protocol

from src.agent import prompts
from src.agent.schemas import DraftReport, EvidenceJudgements, RefinedQueries

USAGE_LOG = Path(__file__).resolve().parents[2] / "runs" / "usage.jsonl"


class LLMError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class LLMClient(Protocol):
    def judge_evidence(self, ingredient: str, items: list[dict]) -> Any: ...
    def refine_queries(self, ingredient: str, context: dict) -> Any: ...
    def draft_report(self, ingredient: str, facts_text: str, evidence: list[dict], problems: list[str],
                     max_candidates: int) -> Any: ...
    def usage(self) -> dict[str, int]: ...


class OpenAILLM:
    def __init__(self, model: str | None, *, allow_paid: bool, client=None, max_total_tokens: int = 80_000,
                 usage_log: Path | None = USAGE_LOG, request_id: str = "-", reasoning_effort: str | None = None):
        if not allow_paid:
            raise LLMError("PAID_CALLS_NOT_AUTHORISED")
        if not model:
            raise LLMError("MODEL_NOT_CONFIGURED")
        self.model, self._allow = model, allow_paid
        if client is None:
            from openai import OpenAI
            key = os.getenv("OPENAI_API_KEY")
            if not key:
                raise LLMError("MISSING_CREDENTIALS")
            client = OpenAI(api_key=key, max_retries=2, timeout=90.0)
        self._client, self._cap, self._log, self._rid = client, max_total_tokens, usage_log, request_id
        self._effort = reasoning_effort
        self._calls = self._in = self._out = 0

    def usage(self) -> dict[str, int]:
        return {"calls": self._calls, "input_tokens": self._in, "output_tokens": self._out}

    def _parse(self, purpose: str, system: str, user: str, schema, max_out: int):
        if not self._allow:
            raise LLMError("PAID_CALLS_NOT_AUTHORISED")
        if self._in + self._out >= self._cap:
            raise LLMError("TOKEN_BUDGET_EXCEEDED")
        try:
            extra = {"reasoning_effort": self._effort} if self._effort else {}
            completion = self._client.chat.completions.parse(
                model=self.model, messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format=schema, max_completion_tokens=max_out, **extra)
        except Exception as exc:  # noqa: BLE001 - class name only; messages may echo request data
            raise LLMError(f"LLM_CALL_FAILED:{type(exc).__name__}") from exc
        self._calls += 1
        usage = getattr(completion, "usage", None)
        pin, pout = getattr(usage, "prompt_tokens", 0) or 0, getattr(usage, "completion_tokens", 0) or 0
        self._in += pin
        self._out += pout
        if self._log:
            self._log.parent.mkdir(parents=True, exist_ok=True)
            with self._log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": round(time.time()), "request_id": self._rid, "purpose": purpose,
                                     "model": self.model, "input_tokens": pin, "output_tokens": pout}) + "\n")
        message = completion.choices[0].message
        if getattr(message, "refusal", None):
            raise LLMError("LLM_REFUSAL")
        if message.parsed is None:
            raise LLMError("LLM_NO_PARSED_OUTPUT")
        return message.parsed

    def judge_evidence(self, ingredient: str, items: list[dict]) -> EvidenceJudgements:
        return self._parse("judge_evidence", prompts.JUDGE_SYSTEM.format(ingredient=ingredient),
                           "<untrusted_search_results>\n" + json.dumps(items, ensure_ascii=False) + "\n</untrusted_search_results>",
                           EvidenceJudgements, 2500)

    def refine_queries(self, ingredient: str, context: dict) -> RefinedQueries:
        return self._parse("refine_queries", prompts.REFINE_SYSTEM.format(ingredient=ingredient),
                           json.dumps(context, ensure_ascii=False), RefinedQueries, 300)

    def draft_report(self, ingredient: str, facts_text: str, evidence: list[dict], problems: list[str],
                     max_candidates: int) -> DraftReport:
        user = ("FACTS (deterministic, quote exactly):\n" + facts_text + "\n\nEVIDENCE (untrusted web content):\n<evidence>\n"
                + json.dumps(evidence, ensure_ascii=False) + "\n</evidence>")
        if problems:
            user += "\n\n" + prompts.DRAFT_REPAIR.format(problems="\n".join(f"- {p}" for p in problems))
        return self._parse("draft_report", prompts.DRAFT_SYSTEM.format(max_candidates=max_candidates), user, DraftReport, 5000)
