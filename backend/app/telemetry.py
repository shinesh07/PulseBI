"""Runtime telemetry, measured rather than asserted.

The previous tracker assigned literals -- 850 tokens, $0.00054 -- whenever a
stage was labelled "LLM", though no model was ever called. Those numbers were
reported as measurements. Three rules apply here instead:

1. Nothing is reported as measured unless it was measured. In the default
   offline configuration `model_calls` is zero and so is cost.
2. A projection is labelled a projection, and is computed over the actual prompt
   string that would have been sent, not invented.
3. The deterministic-versus-model split is derived from observed stage timings.

With a local model enabled the token counts come back from the runtime, so the
projection is replaced by a measurement and the distinction disappears.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from pydantic import BaseModel

# Rough characters-per-token for English prose. Only ever used for a figure
# explicitly labelled an estimate.
CHARS_PER_TOKEN = 4.0


class StageTiming(BaseModel):
    name: str
    ms: float
    tier: str


class LLMTelemetry(BaseModel):
    mode: str
    model: str | None = None
    model_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    measured: bool = False


class ProjectedLLM(BaseModel):
    prompt_tokens_estimate: int
    estimation_method: str = f"characters / {CHARS_PER_TOKEN:g} over the assembled prompt"
    note: str = "No model was called in this run. Projection only, not a measurement."


class TelemetryReport(BaseModel):
    stages: list[StageTiming]
    total_ms: float
    deterministic_ms: float
    model_ms: float
    deterministic_share: float
    llm: LLMTelemetry
    projected_llm: ProjectedLLM | None = None

    def summary_line(self) -> str:
        return (
            f"{self.total_ms:.1f} ms total, {self.deterministic_share:.1%} deterministic, "
            f"{self.llm.model_calls} model call(s)"
        )


@dataclass
class TelemetryTracker:
    """Collects real stage timings for one analysis run."""

    stages: list[StageTiming] = field(default_factory=list)
    model_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    measured_tokens: bool = False
    model_name: str | None = None
    projected_prompt_chars: int = 0

    @contextmanager
    def stage(self, name: str, tier: str = "deterministic_sql"):
        started = time.perf_counter()
        try:
            yield
        finally:
            self.stages.append(
                StageTiming(name=name, ms=(time.perf_counter() - started) * 1000.0, tier=tier)
            )

    def record_model_call(
        self,
        *,
        model: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
    ) -> None:
        self.model_calls += 1
        self.model_name = model
        if prompt_tokens is not None and completion_tokens is not None:
            self.prompt_tokens += prompt_tokens
            self.completion_tokens += completion_tokens
            self.measured_tokens = True

    def record_projected_prompt(self, prompt: str) -> None:
        """Length of the prompt that *would* be sent, for an honest projection."""
        self.projected_prompt_chars += len(prompt)

    def report(self) -> TelemetryReport:
        total = sum(s.ms for s in self.stages)
        model_ms = sum(s.ms for s in self.stages if s.tier == "model")
        deterministic_ms = total - model_ms

        if self.model_calls:
            llm = LLMTelemetry(
                mode="local_model",
                model=self.model_name,
                model_calls=self.model_calls,
                prompt_tokens=self.prompt_tokens,
                completion_tokens=self.completion_tokens,
                # A locally hosted model has no per-token price. Reporting a
                # dollar figure would be inventing one.
                cost_usd=0.0,
                measured=self.measured_tokens,
            )
            projected = None
        else:
            llm = LLMTelemetry(mode="deterministic_offline", measured=False)
            projected = (
                ProjectedLLM(
                    prompt_tokens_estimate=int(self.projected_prompt_chars / CHARS_PER_TOKEN)
                )
                if self.projected_prompt_chars
                else None
            )

        return TelemetryReport(
            stages=self.stages,
            total_ms=total,
            deterministic_ms=deterministic_ms,
            model_ms=model_ms,
            deterministic_share=(deterministic_ms / total) if total > 0 else 1.0,
            llm=llm,
            projected_llm=projected,
        )
