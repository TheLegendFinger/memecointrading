"""Doctor tests - the diagnostics must be right about what is and isn't working."""

import pytest

from memebot.data.dexscreener import DexScreenerClient
from memebot.data.jupiter import JupiterClient
from memebot.doctor import FAIL, OK, WARN, format_report, run_checks
from memebot.http import HttpError
from memebot.models import USDC_MINT, WSOL_MINT
from tests.test_data import FakeHttp, raw_pair


def healthy_clients():
    dex = DexScreenerClient(http=FakeHttp({
        "/latest/dex/search": {"pairs": [raw_pair()]},
        "/token-boosts": [{"chainId": "solana", "tokenAddress": "mint-boosted"}],
        "/token-profiles": [{"chainId": "solana", "tokenAddress": "mint-new"}],
        "/token-pairs/v1": [raw_pair("USDC", USDC_MINT, liquidity=5_000_000.0)],
    }), cache_ttl_seconds=0.0)
    jup = JupiterClient(http=FakeHttp({
        "price": {"data": {WSOL_MINT: {"price": "152.31"}}},
        "/quote": {
            "inputMint": WSOL_MINT, "inAmount": "100000000", "outputMint": USDC_MINT,
            "outAmount": "15200000", "otherAmountThreshold": "15000000",
            "priceImpactPct": "0.0001", "slippageBps": 100,
            "routePlan": [{"swapInfo": {"label": "Whirlpool"}}],
        },
    }))
    return dex, jup


@pytest.fixture
def armed(monkeypatch):
    """An armed, working executor - so checks under test are the ones failing."""
    monkeypatch.setenv("LIVE_TRADING_CONFIRM", "I_UNDERSTAND_THE_RISK")
    monkeypatch.setattr("memebot.execution.live.LiveExecutor.preflight",
                        lambda self, require_arming=True: None)
    monkeypatch.setattr("memebot.execution.live.LiveExecutor.describe",
                        lambda self: "live via Jupiter (test)")


def status_of(report, name):
    return next(c.status for c in report.checks if c.name == name)


def test_everything_reachable_reports_healthy(config, monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_CONFIRM", "I_UNDERSTAND_THE_RISK")
    monkeypatch.setattr("memebot.execution.live.LiveExecutor.preflight",
                        lambda self, require_arming=True: None)
    monkeypatch.setattr("memebot.execution.live.LiveExecutor.describe",
                        lambda self: "live via Jupiter (test)")
    dex, jup = healthy_clients()
    config.filters.min_liquidity_usd = 1_000       # let the sample pair through
    config.filters.min_volume_h24_usd = 1_000
    config.strategy.min_score = 0.0

    report = run_checks(config, deep=True, data=dex, jupiter=jup)

    assert report.healthy
    assert status_of(report, "dexscreener search") == OK
    assert status_of(report, "jupiter price") == OK
    assert status_of(report, "jupiter routing") == OK
    assert status_of(report, "candidate pipeline") == OK
    assert "SOL = $152.31" in format_report(report)


def test_unreachable_dexscreener_is_a_failure_with_the_real_cause(config, armed):
    dex = DexScreenerClient(http=FakeHttp({"/": HttpError("Tunnel connection failed: 403", 403)}))
    _dex, jup = healthy_clients()

    report = run_checks(config, deep=False, data=dex, jupiter=jup)

    assert not report.healthy
    search = next(c for c in report.checks if c.name == "dexscreener search")
    assert search.status == FAIL
    assert "403" in search.detail, "the underlying transport error must reach the report"


def test_price_endpoint_shape_change_is_diagnosed(config, armed):
    dex, _jup = healthy_clients()
    # Endpoint answers 200 but with a shape we cannot read.
    jup = JupiterClient(http=FakeHttp({"price": {"unexpected": "shape"}}))

    report = run_checks(config, deep=False, data=dex, jupiter=jup)
    price = next(c for c in report.checks if c.name == "jupiter price")
    assert price.status == FAIL
    assert "shape changed" in price.detail


def test_filters_rejecting_everything_is_a_warning_not_a_failure(config, armed):
    dex, jup = healthy_clients()
    config.filters.min_liquidity_usd = 10_000_000_000  # nothing can pass

    report = run_checks(config, deep=False, data=dex, jupiter=jup)
    pipeline = next(c for c in report.checks if c.name == "candidate pipeline")
    assert pipeline.status == WARN
    assert "loosen them" in pipeline.detail
    assert report.healthy, "a too-strict filter is a config problem, not an outage"


def test_min_score_too_high_names_the_number_that_would_have_worked(config, armed):
    """"Lower min_score" without a number sent people hunting blindly."""
    dex, jup = healthy_clients()
    config.filters.min_liquidity_usd = 1_000
    config.filters.min_volume_h24_usd = 1_000
    config.strategy.min_score = 0.99

    report = run_checks(config, deep=False, data=dex, jupiter=jup)
    pipeline = next(c for c in report.checks if c.name == "candidate pipeline")
    assert pipeline.status == WARN
    assert "best score was" in pipeline.detail
    assert "would have taken it" in pipeline.detail


def test_a_narrow_scan_is_called_out(config, armed):
    """68 coins a cycle is not a market, it is a rounding error."""
    dex, jup = healthy_clients()
    report = run_checks(config, deep=False, data=dex, jupiter=jup)
    pipeline = next(c for c in report.checks if c.name == "candidate pipeline")
    assert "widen data.search_terms" in pipeline.detail


def test_a_setup_that_is_ready_but_not_armed_passes(config, monkeypatch):
    """Arming is an act, not a fault: the menu does it when you press start."""
    monkeypatch.delenv("LIVE_TRADING_CONFIRM", raising=False)
    monkeypatch.setattr("memebot.execution.live.LiveExecutor.preflight",
                        lambda self, require_arming=True: None)
    monkeypatch.setattr("memebot.execution.live.LiveExecutor.describe",
                        lambda self: "live via Jupiter (test)")
    dex, jup = healthy_clients()

    report = run_checks(config, deep=False, data=dex, jupiter=jup)
    execution = next(c for c in report.checks if c.name == "execution")
    assert execution.status == OK
    assert "arms itself when you start trading" in execution.detail


def test_the_health_check_reports_what_is_actually_wrong(config, monkeypatch):
    """Unarmed used to mask this: no wallet is the thing to say."""
    monkeypatch.delenv("LIVE_TRADING_CONFIRM", raising=False)
    dex, jup = healthy_clients()

    report = run_checks(config, deep=False, data=dex, jupiter=jup)
    execution = next(c for c in report.checks if c.name == "execution")
    assert execution.status == FAIL
    assert "wallet" in execution.detail.lower()
    assert "not armed" not in execution.detail


def test_the_engine_still_refuses_to_trade_unarmed(config, monkeypatch):
    """Relaxing the health check must not relax the interlock itself."""
    monkeypatch.delenv("LIVE_TRADING_CONFIRM", raising=False)
    from memebot.execution.live import LiveExecutor

    executor = LiveExecutor.__new__(LiveExecutor)
    assert "not armed" in LiveExecutor.preflight(executor)


def test_state_store_is_probed(config, armed):
    dex, jup = healthy_clients()
    report = run_checks(config, deep=False, data=dex, jupiter=jup)
    state = next(c for c in report.checks if c.name == "state store")
    assert state.status == OK
    assert "0 open position" in state.detail


def test_report_formatting_lists_every_check(config, armed):
    dex, jup = healthy_clients()
    text = format_report(run_checks(config, deep=False, data=dex, jupiter=jup))
    for name in ("config", "state store", "dexscreener search", "jupiter price"):
        assert name in text
    assert "[PASS]" in text
