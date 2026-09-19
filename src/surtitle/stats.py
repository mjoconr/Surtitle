"""Usage accounting for the running server.

``/api/status`` and the Windows tray icon both answer the question "what has this
process actually done?", and they must answer it identically — a tray that
disagrees with the API is worse than no tray. So the counters live here, in one
place, and are written from the two points in :class:`~surtitle.core.session.Session`
that already see every event and every usage block.

Everything here is *since this process started*. The counts are deliberately not
persisted: the question the tray answers is about the run in front of the user,
and a number that survives a restart raises a question ("since when?") that has
no good answer on a taskbar tooltip. The database has the durable history.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from surtitle.config import Settings
from surtitle.core.events import EventKind

__all__ = [
    "MODEL_PRICES",
    "PEAK_MULTIPLIER",
    "PRICES_CHECKED",
    "PRICES_SOURCE",
    "ModelPrice",
    "RunStats",
    "cost_of",
    "is_peak",
    "resolve_price",
]

# Published rates, in USD per million tokens, taken from the vendor's pricing
# page on the date below. Verified against `deepseek-flash` and `deepseek-v4-pro`,
# which are the two identifiers `PROVIDER_SPECS` offers.
PRICES_CHECKED = "2026-09-14"
PRICES_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing"


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Off-peak rates for one model, in USD per million tokens.

    The vendor publishes peak and off-peak rates and states that off-peak is
    exactly half of peak, so the peak table is derived rather than duplicated —
    one number per column cannot drift out of step with the other.
    """

    input_miss: float
    input_hit: float
    output: float


# Peak hours are 01:00-04:00 and 06:00-10:00 UTC, Monday to Friday; every other
# hour is off-peak. Kept as half-open ranges so the boundaries cannot be counted
# twice.
_PEAK_WINDOWS_UTC: tuple[tuple[int, int], ...] = ((1, 4), (6, 10))
PEAK_MULTIPLIER = 2.0

_FLASH = ModelPrice(input_miss=0.15, input_hit=0.003, output=0.60)
_PRO = ModelPrice(input_miss=0.66, input_hit=0.022, output=1.98)

MODEL_PRICES: dict[str, ModelPrice] = {
    "deepseek-flash": _FLASH,
    "deepseek-v4-pro": _PRO,
    # Retired identifiers that the vendor still accepts and serves from the
    # Flash model. They bill at Flash rates, so they price at Flash rates.
    "deepseek-v4-flash": _FLASH,
    "deepseek-v4-flash-vision-exp": _FLASH,
}

# How much context each model accepts, in tokens, from the same page as the
# prices.
#
# It has to be a table because no API reports it: `GET /models` returns an id, an
# object type and an owner, and nothing else. The window is the one number the
# browser's budget meter is measured against, so a stale value is not a cosmetic
# problem — a 128k default against a 1M window read "55% full" at 7%.
#
# An unknown model deliberately has no window: the meter hides itself rather than
# picking a number, and `SURTITLE_CONTEXT_LIMIT` is the way to state one.
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "deepseek-flash": 1_000_000,
    "deepseek-v4-pro": 1_000_000,
    # Retired identifiers, served by the Flash model — same window.
    "deepseek-v4-flash": 1_000_000,
    "deepseek-v4-flash-vision-exp": 1_000_000,
}


def is_peak(moment: datetime | None = None) -> bool:
    """True when ``moment`` (UTC, default now) falls in a published peak window."""
    when = moment or datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    if when.weekday() >= 5:  # Saturday or Sunday
        return False
    return any(start <= when.hour < end for start, end in _PEAK_WINDOWS_UTC)


def resolve_price(model: str, settings: Settings | None = None) -> ModelPrice | None:
    """Price table for ``model``, with any ``SURTITLE_PRICE_*`` override applied.

    Returns ``None`` for a model that is in no table. Callers must then report
    tokens without a money figure: a wrong cost is worse than an absent one, and
    a model can be added to the account before it is added here.
    """
    price = MODEL_PRICES.get((model or "").strip().lower())
    if settings is None:
        return price

    overrides = (
        settings.price_input_per_mtok,
        settings.price_cached_input_per_mtok,
        settings.price_output_per_mtok,
    )
    if all(value is None for value in overrides):
        return price
    if price is None:
        # An override can price a model this build has never heard of, but only
        # if every column is supplied; a partial override has nothing to fall
        # back to and must not silently become zero.
        if any(value is None for value in overrides):
            return None
        return ModelPrice(*overrides)  # type: ignore[arg-type]
    return ModelPrice(
        input_miss=overrides[0] if overrides[0] is not None else price.input_miss,
        input_hit=overrides[1] if overrides[1] is not None else price.input_hit,
        output=overrides[2] if overrides[2] is not None else price.output,
    )


def context_window(model: str, settings: Settings | None = None) -> int:
    """How much context ``model`` accepts, in tokens; ``0`` when unknown.

    The table is the answer and ``SURTITLE_CONTEXT_LIMIT`` overrides it, because
    a new model can reach the account before it reaches this file. Zero means
    "say nothing": the browser hides the meter rather than measuring against a
    number nobody knows, which is the failure this replaced — the meter read
    "55% full" against a 128k window that was eight times too small.
    """
    override = int(getattr(settings, "context_limit", 0) or 0)
    if override > 0:
        return override
    return MODEL_CONTEXT_WINDOWS.get((model or "").strip().lower(), 0)


def cost_of(
    counts: dict[str, int],
    price: ModelPrice | None,
    *,
    peak: bool = False,
) -> float:
    """Cost in USD of one usage block against a rate card.

    Zero when there is no rate card, which the caller distinguishes by checking
    the price itself rather than by testing for zero.

    ``reasoning_tokens`` are *not* billed separately: the vendor counts them
    inside ``completion_tokens``, and adding them again would double the figure
    for exactly the thinking-heavy turns that cost the most.
    """
    if price is None:
        return 0.0
    multiplier = PEAK_MULTIPLIER if peak else 1.0
    prompt = int(counts.get("prompt_tokens") or 0)
    # A malformed block must not produce a negative cost: cache hits are a
    # subset of the prompt, and a server that reports otherwise is not a reason
    # to bill a negative amount.
    cached = min(int(counts.get("cached_tokens") or 0), prompt)
    billable_input = prompt - cached
    completion = int(counts.get("completion_tokens") or 0)
    return (
        multiplier
        * (billable_input * price.input_miss + cached * price.input_hit + completion * price.output)
        / 1_000_000
    )


@dataclass(slots=True)
class RunStats:
    """Counters for one server process.

    Written from the event stream and the completion loop, read from the HTTP
    status route, which runs on the same event loop — but also read by the tray
    thread if it is ever handed this object directly, so the lock is real rather
    than decorative.
    """

    started_at: float = field(default_factory=time.time)
    model: str = ""
    # One per model completion, which is not the same as one per turn: a
    # tool-using turn calls the model once per step.
    model_calls: int = 0
    turns: int = 0
    tool_calls: int = 0
    errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    # False once a completion was priced against a model this build has no rates
    # for. The tray then shows tokens with no money figure rather than a total
    # that quietly omits part of the run.
    priced: bool = True
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def record_event(self, kind: EventKind | str) -> None:
        """Count one event that reached the browser."""
        name = kind.value if isinstance(kind, EventKind) else str(kind)
        with self._lock:
            if name == EventKind.TOOL_CALL.value:
                self.tool_calls += 1
            elif name == EventKind.DONE.value:
                self.turns += 1
            elif name == EventKind.ERROR.value:
                self.errors += 1

    def record_usage(
        self,
        counts: dict[str, Any],
        *,
        price: ModelPrice | None = None,
        peak: bool | None = None,
        moment: datetime | None = None,
    ) -> None:
        """Fold one completion's usage block into the run totals."""
        when = moment or datetime.now(UTC)
        at_peak = is_peak(when) if peak is None else peak
        with self._lock:
            self.model_calls += 1
            self.prompt_tokens += int(counts.get("prompt_tokens") or 0)
            self.completion_tokens += int(counts.get("completion_tokens") or 0)
            self.reasoning_tokens += int(counts.get("reasoning_tokens") or 0)
            self.cached_tokens += int(counts.get("cached_tokens") or 0)
            if price is None:
                self.priced = False
            else:
                self.cost_usd += cost_of(counts, price, peak=at_peak)

    @property
    def uptime_seconds(self) -> float:
        return max(0.0, time.time() - self.started_at)

    def snapshot(self, *, model: str | None = None) -> dict[str, Any]:
        """Serialisable view, safe to hand to the API or to the tray."""
        with self._lock:
            return {
                "model": model or self.model,
                "started_at": self.started_at,
                "uptime_seconds": self.uptime_seconds,
                "model_calls": self.model_calls,
                "turns": self.turns,
                "tool_calls": self.tool_calls,
                "errors": self.errors,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "reasoning_tokens": self.reasoning_tokens,
                "cached_tokens": self.cached_tokens,
                "total_tokens": self.prompt_tokens + self.completion_tokens,
                # Both are reported even when unusable, so the tray can say
                # "unpriced" rather than showing a confident $0.00.
                "cost_usd": round(self.cost_usd, 6),
                "priced": self.priced,
                "price_source": PRICES_SOURCE,
                "price_checked": PRICES_CHECKED,
            }


def format_duration(seconds: float) -> str:
    """Compact ``1h 04m`` / ``12m 30s`` / ``8s`` rendering of an interval."""
    total = int(max(0.0, seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"
