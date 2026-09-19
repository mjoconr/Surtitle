"""Usage accounting: the numbers behind the tray icon and ``/api/status``.

These are worth testing precisely because they are the only part of the feature a
user cannot check for themselves. A wrong turn count is annoying; a wrong money
figure is a claim about their invoice, and the pricing table has two rates per
column, a cache-hit split and a reasoning-token subtlety for it to get wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from surtitle.config import Settings
from surtitle.core.events import EventKind
from surtitle.stats import (
    MODEL_PRICES,
    ModelPrice,
    RunStats,
    context_window,
    cost_of,
    format_duration,
    is_peak,
    resolve_price,
)


@pytest.fixture
def settings(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return Settings(DEEPSEEK_API_KEY="sk-test", SURTITLE_HOME=str(home))


class TestRateCard:
    def test_every_offered_model_is_priced(self):
        """A model the app offers but cannot price shows tokens and no money."""
        offered = {"deepseek-flash", "deepseek-v4-pro"}
        assert offered <= set(MODEL_PRICES)

    def test_peak_windows_are_weekday_utc(self):
        # 02:00 UTC on a Tuesday is inside the first published peak window.
        assert is_peak(datetime(2026, 9, 15, 2, 0, tzinfo=UTC)) is True
        # 05:00 UTC is between the two windows.
        assert is_peak(datetime(2026, 9, 15, 5, 0, tzinfo=UTC)) is False
        # The same hour on a Saturday is off-peak all day.
        assert is_peak(datetime(2026, 9, 19, 2, 0, tzinfo=UTC)) is False

    def test_peak_costs_exactly_double_off_peak(self):
        counts = {"prompt_tokens": 1000, "completion_tokens": 500}
        price = ModelPrice(input_miss=1.0, input_hit=0.1, output=2.0)
        off = cost_of(counts, price, peak=False)
        peak = cost_of(counts, price, peak=True)
        assert peak == pytest.approx(off * 2)

    def test_cache_hits_are_billed_at_the_cache_rate(self):
        price = ModelPrice(input_miss=1.0, input_hit=0.0, output=0.0)
        # All 1000 prompt tokens were cached: at a zero cache rate this is free,
        # which is the whole reason the split has to be applied at all.
        assert cost_of({"prompt_tokens": 1000, "cached_tokens": 1000}, price) == 0.0

    def test_more_cached_than_prompted_cannot_go_negative(self):
        """A malformed usage block must not credit the account."""
        price = ModelPrice(input_miss=1.0, input_hit=0.5, output=1.0)
        assert cost_of({"prompt_tokens": 10, "cached_tokens": 999}, price) > 0

    def test_reasoning_tokens_are_not_billed_twice(self):
        """They are a subset of completion tokens, and the vendor says so."""
        price = ModelPrice(input_miss=1.0, input_hit=1.0, output=10.0)
        plain = cost_of({"completion_tokens": 1000}, price)
        with_reasoning = cost_of({"completion_tokens": 1000, "reasoning_tokens": 800}, price)
        assert with_reasoning == plain

    def test_no_price_means_no_estimate(self):
        assert cost_of({"prompt_tokens": 1000}, None) == 0.0


class TestResolution:
    def test_known_model_resolves(self):
        assert resolve_price("deepseek-flash") is not None

    def test_model_name_is_case_insensitive(self):
        assert resolve_price("DeepSeek-Flash") == resolve_price("deepseek-flash")

    def test_unknown_model_has_no_price(self):
        assert resolve_price("some-future-model") is None

    def test_environment_override_wins(self, tmp_path):
        settings = Settings(
            DEEPSEEK_API_KEY="sk-test",
            SURTITLE_HOME=str(tmp_path),
            SURTITLE_PRICE_INPUT=9.0,
            SURTITLE_PRICE_CACHED_INPUT=1.0,
            SURTITLE_PRICE_OUTPUT=90.0,
        )
        price = resolve_price("deepseek-flash", settings)
        assert price == ModelPrice(input_miss=9.0, input_hit=1.0, output=90.0)

    def test_partial_override_keeps_published_rates(self, tmp_path):
        settings = Settings(
            DEEPSEEK_API_KEY="sk-test",
            SURTITLE_HOME=str(tmp_path),
            SURTITLE_PRICE_OUTPUT=99.0,
        )
        published = MODEL_PRICES["deepseek-flash"]
        price = resolve_price("deepseek-flash", settings)
        assert price is not None
        assert price.output == 99.0
        assert price.input_miss == published.input_miss

    def test_an_unknown_model_needs_every_column(self, tmp_path):
        """Half a rate card must not price a model at the columns it has."""
        partial = Settings(
            DEEPSEEK_API_KEY="sk-test",
            SURTITLE_HOME=str(tmp_path),
            SURTITLE_PRICE_INPUT=1.0,
        )
        assert resolve_price("some-future-model", partial) is None
        complete = Settings(
            DEEPSEEK_API_KEY="sk-test",
            SURTITLE_HOME=str(tmp_path),
            SURTITLE_PRICE_INPUT=1.0,
            SURTITLE_PRICE_CACHED_INPUT=0.1,
            SURTITLE_PRICE_OUTPUT=2.0,
        )
        assert resolve_price("some-future-model", complete) is not None


class TestRunStats:
    def test_counts_the_events_the_tray_reports(self):
        stats = RunStats()
        stats.record_event(EventKind.TOOL_CALL)
        stats.record_event(EventKind.TOOL_CALL)
        stats.record_event(EventKind.DONE)
        stats.record_event(EventKind.ERROR)
        # Events that are neither must not be counted as any of them.
        stats.record_event(EventKind.THINKING)
        snapshot = stats.snapshot()
        assert snapshot["tool_calls"] == 2
        assert snapshot["turns"] == 1
        assert snapshot["errors"] == 1

    def test_accepts_plain_strings_as_well_as_enum_members(self):
        stats = RunStats()
        stats.record_event("tool_call")
        assert stats.snapshot()["tool_calls"] == 1

    def test_usage_accumulates_across_calls(self):
        stats = RunStats()
        price = ModelPrice(input_miss=1.0, input_hit=0.0, output=1.0)
        stats.record_usage({"prompt_tokens": 100, "completion_tokens": 10}, price=price, peak=False)
        stats.record_usage({"prompt_tokens": 200, "completion_tokens": 20}, price=price, peak=False)
        snapshot = stats.snapshot()
        assert snapshot["model_calls"] == 2
        assert snapshot["prompt_tokens"] == 300
        assert snapshot["completion_tokens"] == 30
        assert snapshot["total_tokens"] == 330

    def test_unpriced_run_is_flagged_rather_than_shown_as_free(self):
        stats = RunStats()
        stats.record_usage({"prompt_tokens": 500}, price=None)
        snapshot = stats.snapshot()
        assert snapshot["priced"] is False
        assert snapshot["cost_usd"] == 0.0

    def test_a_later_priced_call_does_not_unflag_the_run(self):
        """Once part of the run is unpriced the total is an understatement."""
        stats = RunStats()
        price = ModelPrice(input_miss=1.0, input_hit=1.0, output=1.0)
        stats.record_usage({"prompt_tokens": 500}, price=None)
        stats.record_usage({"prompt_tokens": 500}, price=price)
        assert stats.snapshot()["priced"] is False

    def test_snapshot_reports_the_model_it_was_asked_about(self):
        assert RunStats().snapshot(model="deepseek-v4-pro")["model"] == "deepseek-v4-pro"

    def test_uptime_is_never_negative(self):
        assert RunStats().uptime_seconds >= 0


class TestDurationFormatting:
    def test_seconds(self):
        assert format_duration(8) == "8s"

    def test_minutes(self):
        assert format_duration(12 * 60 + 30) == "12m 30s"

    def test_hours(self):
        assert format_duration(3600 + 4 * 60) == "1h 04m"

    def test_negative_is_clamped(self):
        assert format_duration(-5) == "0s"


class TestContextWindow:
    """The window is not something the API will tell us.

    `GET /models` returns an id, an object type and an owner, and nothing else —
    so the published table is the answer, and an override is the escape hatch for
    a model that arrives before the table does. This matters more than it sounds:
    measured against a 128k default, a 1M window read "55% full" at 7%.
    """

    def test_the_published_window_for_the_model_in_use(self):
        assert context_window("deepseek-flash") == 1_000_000

    def test_the_retired_identifiers_keep_their_window(self):
        assert context_window("deepseek-v4-flash") == 1_000_000
        assert context_window("deepseek-v4-pro") == 1_000_000

    def test_an_unknown_model_has_no_window(self):
        """Saying nothing beats measuring against a guess."""
        assert context_window("a-model-from-next-year") == 0

    def test_the_configured_override_wins(self):
        configured = Settings(DEEPSEEK_API_KEY="k", SURTITLE_CONTEXT_LIMIT=32_000)

        assert context_window("deepseek-flash", configured) == 32_000
        assert context_window("a-model-from-next-year", configured) == 32_000

    def test_the_default_is_not_a_number_of_its_own(self):
        """A default window is the bug: it is right only until the model changes."""
        assert Settings(DEEPSEEK_API_KEY="k").context_limit == 0
