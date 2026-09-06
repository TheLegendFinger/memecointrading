"""On-chain checks on the token, before any money moves.

The market data this bot trades on cannot see either of the two mechanical rug
setups: a mint authority that is still live (they print more supply and your
share goes to nothing) and a freeze authority that is still live (you hold the
coin and can never sell it). Both are visible on the mint account, and both
have to be read from the chain.
"""

import pytest

from memebot.config import BotConfig, SafetyConfig
from memebot.models import PairSnapshot, Token
from memebot.safety import TokenSafetyChecker


class FakeRpc:
    """Just the three calls the checker makes."""

    def __init__(self, mint_authority=None, freeze_authority=None,
                 supply=1_000_000.0, holders=None, raises=None):
        self.mint_authority = mint_authority
        self.freeze_authority = freeze_authority
        self.supply = supply
        self.holders = holders if holders is not None else [5_000.0, 4_000.0, 3_000.0]
        self.raises = raises
        self.calls = 0

    def get_mint_account(self, mint):
        self.calls += 1
        if self.raises:
            raise self.raises
        return {"mintAuthority": self.mint_authority,
                "freezeAuthority": self.freeze_authority, "decimals": 6}

    def get_token_supply(self, mint):
        return self.supply

    def get_token_largest_accounts(self, mint):
        return list(self.holders)


def make_pair(price=0.001, liquidity=100_000.0):
    return PairSnapshot(
        chain_id="solana", dex_id="raydium", pair_address="pool-1",
        base=Token(address="mint-1", symbol="DOG", name="Dog"),
        quote=Token(address="So11111111111111111111111111111111111111112", symbol="SOL"),
        price_usd=price, liquidity_usd=liquidity,
    )


def check(rpc, **overrides):
    cfg = SafetyConfig(**overrides)
    return TokenSafetyChecker(rpc, cfg).check(make_pair())


# ---- the two that empty a wallet -------------------------------------------------
def test_a_live_mint_authority_is_refused():
    verdict = check(FakeRpc(mint_authority="Deployer111"))
    assert verdict.ok is False
    assert "supply can be inflated" in verdict.summary


def test_a_live_freeze_authority_is_refused():
    """A chart can look perfect right up to the moment you try to sell."""
    verdict = check(FakeRpc(freeze_authority="Deployer111"))
    assert verdict.ok is False
    assert "frozen" in verdict.summary


def test_a_clean_token_passes():
    verdict = check(FakeRpc())
    assert verdict.ok is True
    assert verdict.badge == "ok"
    assert "authorities revoked" in verdict.summary


def test_both_problems_are_reported_not_just_the_first():
    verdict = check(FakeRpc(mint_authority="A", freeze_authority="B"))
    assert len(verdict.reasons) == 2


# ---- holders ---------------------------------------------------------------------
def test_one_account_holding_most_of_the_supply_is_refused():
    verdict = check(FakeRpc(supply=1_000.0, holders=[600.0, 10.0]))
    assert verdict.ok is False
    assert "one account holds 60%" in verdict.summary


def test_ten_accounts_holding_most_of_the_supply_are_refused():
    verdict = check(
        FakeRpc(supply=1_000.0, holders=[80.0] * 10),
        max_single_holder_pct=0.5,
    )
    assert verdict.ok is False
    assert "ten accounts hold 80%" in verdict.summary


def test_the_pool_is_not_counted_as_a_whale():
    """A constant-product pool holds most of the supply by design. Counting it
    as a holder makes every healthy token look captured."""
    pair = make_pair(price=0.001, liquidity=100_000.0)
    pool_tokens = (100_000.0 / 2) / 0.001          # 50,000,000
    rpc = FakeRpc(supply=100_000_000.0, holders=[pool_tokens, 1_000_000.0, 500_000.0])

    verdict = TokenSafetyChecker(rpc, SafetyConfig()).check(pair)

    assert verdict.ok is True
    assert verdict.pool_pct == pytest.approx(0.5)
    assert verdict.top_holder_pct == pytest.approx(0.01)


def test_only_one_account_is_forgiven_for_being_the_pool():
    """Two accounts at pool size is one pool and one whale, not two pools."""
    pair = make_pair(price=0.001, liquidity=100_000.0)
    pool_tokens = 50_000_000.0
    rpc = FakeRpc(supply=100_000_000.0, holders=[pool_tokens, pool_tokens * 0.9])

    verdict = TokenSafetyChecker(rpc, SafetyConfig()).check(pair)

    assert verdict.ok is False
    assert "one account holds 45%" in verdict.summary


def test_holder_limits_can_be_turned_off():
    verdict = check(FakeRpc(supply=1_000.0, holders=[999.0]),
                    max_single_holder_pct=0, max_top10_holder_pct=0)
    assert verdict.ok is True


# ---- when the chain cannot be read -----------------------------------------------
def test_an_unreadable_token_is_not_bought_by_default():
    """The failure mode is total, so an unchecked token is a failed check."""
    verdict = check(FakeRpc(raises=RuntimeError("node is down")))
    assert verdict.ok is False
    assert verdict.checked is False
    assert verdict.badge == "?"


def test_an_unreadable_token_can_be_allowed_deliberately():
    verdict = check(FakeRpc(raises=RuntimeError("node is down")), allow_unverified=True)
    assert verdict.ok is True
    assert verdict.checked is False


def test_a_broken_rpc_never_raises_out_of_the_checker():
    """A safety check that crashes the cycle protects nothing."""
    verdict = check(FakeRpc(raises=KeyboardInterrupt if False else Exception("boom")))
    assert verdict.ok is False


# ---- caching ---------------------------------------------------------------------
def test_the_answer_is_cached_so_a_scan_does_not_hammer_the_node():
    rpc = FakeRpc()
    checker = TokenSafetyChecker(rpc, SafetyConfig())
    pair = make_pair()
    for _ in range(5):
        checker.check(pair)
    assert rpc.calls == 1


def test_the_cache_expires():
    rpc = FakeRpc()
    checker = TokenSafetyChecker(rpc, SafetyConfig(cache_seconds=0))
    pair = make_pair()
    checker.check(pair)
    checker.check(pair)
    assert rpc.calls == 2


# ---- the engine gate -------------------------------------------------------------
@pytest.fixture
def hot_pair():
    from test_engine import make_pair

    return make_pair("BEST", chg_m5=12.0, chg_h1=55.0, vol_m5=60_000, vol_h1=400_000,
                     vol_h24=960_000, buys_m5=90, sells_m5=10, buys_h1=800, sells_h1=200,
                     liquidity=700_000, price=0.01)


def test_the_engine_will_not_buy_a_token_that_fails(config, hot_pair):
    from test_engine import build_engine

    engine, _ = build_engine(config, [hot_pair])
    engine.safety = TokenSafetyChecker(FakeRpc(mint_authority="Deployer111"),
                                       SafetyConfig())

    report = engine.run_cycle()

    assert report.opened == []
    assert any("safety" in reason for reason in report.skipped)


def test_the_engine_buys_a_clean_token(config, hot_pair):
    from test_engine import build_engine

    engine, _ = build_engine(config, [hot_pair])
    engine.safety = TokenSafetyChecker(FakeRpc(), SafetyConfig())

    report = engine.run_cycle()

    assert report.opened, "a clean token is still bought"


def test_the_check_is_skipped_entirely_when_disabled(config, hot_pair):
    from memebot.engine import TradingEngine
    from memebot.storage import Storage
    from conftest import FakeDexScreener

    config.safety.enabled = False
    market = FakeDexScreener([hot_pair])
    engine = TradingEngine(config, storage=Storage(":memory:"), data=market)
    assert engine.safety is None


# ---- what the user sees ----------------------------------------------------------
def test_scan_shows_the_on_chain_verdict(config, hot_pair, monkeypatch, capsys):
    """A refusal you cannot see is a refusal you will not trust."""
    from argparse import Namespace

    from memebot import cli
    from test_engine import build_engine

    engine, _ = build_engine(config, [hot_pair])
    engine.safety = TokenSafetyChecker(FakeRpc(mint_authority="Deployer111"),
                                       SafetyConfig())
    monkeypatch.setattr(cli, "_build_engine", lambda cfg, on_cycle=None: engine)
    monkeypatch.setattr(cli, "discover_candidates", lambda client, cfg: [hot_pair])

    code = cli.cmd_scan(
        Namespace(limit=20, no_safety=False, safety_limit=10), config
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "CHAIN" in out
    assert "RISK" in out
    assert "supply can be inflated" in out
    assert "would be refused on-chain" in out


def test_scan_can_skip_the_chain_checks(config, hot_pair, monkeypatch, capsys):
    from argparse import Namespace

    from memebot import cli
    from test_engine import build_engine

    engine, _ = build_engine(config, [hot_pair])
    rpc = FakeRpc()
    engine.safety = TokenSafetyChecker(rpc, SafetyConfig())
    monkeypatch.setattr(cli, "_build_engine", lambda cfg, on_cycle=None: engine)
    monkeypatch.setattr(cli, "discover_candidates", lambda client, cfg: [hot_pair])

    cli.cmd_scan(Namespace(limit=20, no_safety=True, safety_limit=10), config)

    assert rpc.calls == 0, "no RPC calls when the user opted out"


# ---- when the node will not answer the heavy query -------------------------------
class HolderlessRpc(FakeRpc):
    """Authorities readable, holders refused - what a free public RPC does."""

    def get_token_supply(self, mint):
        from memebot.http import HttpError

        raise HttpError("getTokenLargestAccounts: POST https://api.mainnet-beta."
                        "solana.com failed - HTTP 410: method disabled", 410)

    def get_token_largest_accounts(self, mint):
        return self.get_token_supply(mint)


def test_a_coin_is_still_bought_when_only_the_holders_cannot_be_read():
    """The bug: every candidate was refused because a free RPC would not answer
    getTokenLargestAccounts, so the bot never bought anything at all."""
    verdict = check(HolderlessRpc())

    assert verdict.ok is True
    assert verdict.checked is True
    assert verdict.holders_read is False
    assert "holders unknown" in verdict.summary


def test_holder_data_can_be_made_mandatory():
    verdict = check(HolderlessRpc(), require_holder_data=True)

    assert verdict.ok is False
    assert "holders could not be read" in verdict.summary


def test_the_authorities_still_decide_when_holders_are_missing():
    verdict = check(HolderlessRpc(mint_authority="Deployer111"))

    assert verdict.ok is False
    assert "supply can be inflated" in verdict.summary


def test_an_empty_holder_response_is_not_a_zero_concentration():
    verdict = check(FakeRpc(supply=0.0, holders=[]))

    assert verdict.ok is True
    assert verdict.holders_read is False
    assert verdict.top10_pct == 0.0


def test_the_feed_headline_stays_short():
    """The activity feed truncates at 47 characters, which is how a useful
    error became 'holders could not be read: POST'."""
    verdict = check(HolderlessRpc(mint_authority="Deployer111"))

    assert len(f"Skipped WOJAK: {verdict.headline}") < 47
    assert verdict.headline == "mint authority still active"


def test_the_rpc_names_the_call_that_failed():
    """"POST https://... failed" does not say which query the node refused."""
    from memebot.execution.live import SolanaRpc
    from memebot.http import HttpError

    class Refusing:
        def post(self, url, json_body=None, **kw):
            raise HttpError(f"POST {url} failed - HTTP 410", 410)

    rpc = SolanaRpc("https://rpc.example", http=Refusing())
    with pytest.raises(HttpError, match="getTokenLargestAccounts: POST"):
        rpc.get_token_largest_accounts("mint-1")
