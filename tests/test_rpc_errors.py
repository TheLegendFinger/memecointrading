"""Making a node's refusal readable.

    network error: RPC sendTransaction error: {'code': -3200

Everything that shows an error truncates it, and this one put a raw Python
dict on screen - so the truncation landed mid-code and the whole message was
past the cut. The fix is on both sides: say what the code MEANS, first, and
wrap the rest instead of chopping it.
"""

import pytest

from memebot.console_view import ConsoleView
from memebot.execution.live import SolanaRpc, describe_rpc_error
from memebot.http import HttpError


def simulation_error(message, logs=None):
    error = {"code": -32002, "message": message}
    if logs:
        error["data"] = {"logs": logs}
    return error


# ---- saying what it means --------------------------------------------------------
def test_the_meaning_comes_before_the_machinery():
    """Whatever truncates it, the first words have to be the useful ones."""
    line = describe_rpc_error("sendTransaction", simulation_error("Node is unhappy"))
    assert line.startswith("the node simulated the transaction and it failed")


def test_the_jupiter_slippage_error_is_named():
    """0x1771 is the most common swap failure there is: the price moved."""
    line = describe_rpc_error("sendTransaction", simulation_error(
        "Transaction simulation failed: Error processing Instruction 4: "
        "custom program error: 0x1771"))
    assert line.startswith("slippage tolerance exceeded")
    assert "quote and send" in line


def test_running_out_of_sol_is_named():
    line = describe_rpc_error("sendTransaction", simulation_error(
        "Transaction simulation failed: insufficient lamports 890880, need 2039280"))
    assert line.startswith("not enough SOL")


def test_an_expired_blockhash_is_named():
    line = describe_rpc_error("sendTransaction", simulation_error("Blockhash not found"))
    assert "expired" in line


def test_an_unknown_code_still_says_which_call_and_what_the_node_said():
    line = describe_rpc_error("sendTransaction", {"code": -99999, "message": "kaboom"})
    assert "sendTransaction" in line and "-99999" in line and "kaboom" in line


def test_the_program_log_is_kept_because_it_names_the_instruction():
    line = describe_rpc_error("sendTransaction", simulation_error(
        "Transaction simulation failed", logs=["Program log: Error: SlippageToleranceExceeded"]))
    assert "SlippageToleranceExceeded" in line


def test_a_non_dict_error_does_not_crash_the_report():
    assert "kaboom" in describe_rpc_error("sendTransaction", "kaboom")


def test_no_raw_python_dict_reaches_the_message():
    line = describe_rpc_error("sendTransaction", simulation_error("boom"))
    assert "{'code'" not in line and "{" not in line


def test_the_rpc_raises_the_readable_form():
    class Node:
        def post(self, url, json_body=None, **kw):
            return {"jsonrpc": "2.0", "id": 1,
                    "error": simulation_error(
                        "Transaction simulation failed: custom program error: 0x1771")}

    rpc = SolanaRpc("https://rpc.example", http=Node())
    with pytest.raises(HttpError, match="slippage tolerance exceeded"):
        rpc.send_raw_transaction("deadbeef")


# ---- and not chopping it ---------------------------------------------------------
def test_a_long_detail_wraps_instead_of_being_cut():
    rows = ConsoleView._wrap(
        "slippage tolerance exceeded - the price moved between quote and send - "
        "sendTransaction [-32002]: Transaction simulation failed", 56, limit=2)
    assert len(rows) == 2
    assert all(len(r) <= 56 for r in rows)
    assert "slippage tolerance exceeded" in rows[0]
    assert "sendTransaction" in " ".join(rows)


def test_a_short_detail_is_left_alone():
    assert ConsoleView._wrap("stop loss", 56) == ["stop loss"]


def test_what_does_not_fit_is_marked_as_trimmed():
    """A cut message must not read as a complete one."""
    rows = ConsoleView._wrap("word " * 60, 56, limit=2)
    assert len(rows) == 2
    assert rows[-1].endswith(("…", "."))


def test_the_feed_shows_the_whole_reason(config):
    from memebot.storage import Storage

    storage = Storage(":memory:")
    storage.record_event(
        "error", "Could not sell SOLCAT", level="error",
        detail="slippage tolerance exceeded - the price moved between quote and "
               "send - sendTransaction [-32002]",
    )

    class Engine:
        pass

    engine = Engine()
    engine.storage = storage
    lines = ConsoleView(force=True).activity(engine)
    body = "\n".join(lines)

    assert "slippage tolerance exceeded" in body
    assert "-32002" in body, "the code survives to the screen now"


# ---- trying again, but only when it is provably safe -----------------------------
class CountingExecutor:
    """A LiveExecutor with _execute replaced, to watch the retry decision."""

    def __init__(self, config, errors):
        from memebot.execution.live import LiveExecutor

        self.executor = LiveExecutor.__new__(LiveExecutor)
        self.executor.cfg = config.execution
        self.executor.config = config
        self.executor.preflight = lambda require_arming=True: None
        self.errors = list(errors)
        self.calls = 0
        self.executor._execute = self._execute

    def _execute(self, order):
        from memebot.models import Fill

        self.calls += 1
        if self.errors:
            raise HttpError(self.errors.pop(0))
        return Fill(order=order, ok=True, price=1.0, token_amount=1.0, usd_amount=1.0)

    def execute(self, order):
        return self.executor.execute(order)


def sell_order():
    from memebot.models import Order, Side, Token

    return Order(token=Token(address="mint-1", symbol="X"), side=Side.SELL,
                 reference_price=1.0, token_amount=10.0)


def test_a_simulation_refusal_is_tried_again_with_a_fresh_quote(config):
    """Nothing reached the network, so nothing can be double-spent."""
    runner = CountingExecutor(config, [
        "slippage tolerance exceeded - sendTransaction [-32002]: simulation failed",
    ])

    fill = runner.execute(sell_order())

    assert fill.ok is True
    assert runner.calls == 2


def test_anything_that_might_be_in_flight_is_not_retried(config):
    """A resend could double-spend, so a timeout gets reported, not repeated."""
    runner = CountingExecutor(config, ["sendTransaction: POST https://rpc failed - timeout"])

    fill = runner.execute(sell_order())

    assert fill.ok is False
    assert runner.calls == 1, "not retried"
    assert "timeout" in fill.error


def test_it_gives_up_after_one_extra_attempt(config):
    runner = CountingExecutor(config, ["[-32002] simulation failed"] * 5)

    fill = runner.execute(sell_order())

    assert fill.ok is False
    assert runner.calls == 2
    assert "-32002" in fill.error


def test_the_requote_can_be_switched_off(config):
    config.execution.requote_on_preflight_failure = False
    runner = CountingExecutor(config, ["[-32002] simulation failed"])

    runner.execute(sell_order())

    assert runner.calls == 1


@pytest.mark.parametrize("message, retryable", [
    ("slippage tolerance exceeded - sendTransaction [-32002]", True),
    ("the node simulated the transaction and it failed - sendTransaction [-32002]", True),
    ("Transaction simulation failed: custom program error", True),
    ("sendTransaction: POST https://rpc failed - HTTP 429", False),
    ("the node is behind or rate-limiting this wallet - sendTransaction [-32005]", False),
    ("signature verification failed - sendTransaction [-32003]", False),
])
def test_only_pre_flight_refusals_count_as_safe_to_repeat(message, retryable):
    from memebot.execution.live import is_preflight_failure

    assert is_preflight_failure(message) is retryable


# ---- a route the program refuses is not a stale price ----------------------------
def program_error(hex_code):
    return {"code": -32002, "message": "Transaction simulation failed",
            "data": {"logs": [f"Program JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4 "
                              f"failed: custom program error: {hex_code}"]}}


def test_an_unknown_program_error_is_reported_by_number_not_guessed_at():
    """0x1788 is Jupiter error 6024. Naming it from memory would be inventing
    a fact; the number is enough to look it up."""
    line = describe_rpc_error("sendTransaction", program_error("0x1788"))

    assert "Jupiter error 6024" in line and "0x1788" in line
    assert "this route will keep failing" in line


def test_a_refused_route_is_not_re_quoted():
    """The bug in the report: every -32002 was treated as a stale price, so a
    route the program had refused was quoted again and refused again."""
    from memebot.execution.live import is_preflight_failure, is_worth_requoting

    line = describe_rpc_error("sendTransaction", program_error("0x1788"))

    assert is_preflight_failure(line) is True, "nothing reached the network"
    assert is_worth_requoting(line) is False, "but the same route will fail again"


def test_a_stale_price_is_still_re_quoted():
    from memebot.execution.live import is_worth_requoting

    line = describe_rpc_error("sendTransaction", program_error("0x1771"))
    assert is_worth_requoting(line) is True


def test_a_plain_simulation_failure_is_re_quoted():
    from memebot.execution.live import is_worth_requoting

    line = describe_rpc_error("sendTransaction", simulation_error("Transaction too large"))
    assert is_worth_requoting(line) is True


@pytest.mark.parametrize("text, expected", [
    ("custom program error: 0x1788", "0x1788"),
    ("custom program error:0X1771", "0x1771"),
    ("nothing here", None),
])
def test_the_hex_code_is_pulled_out_of_the_message(text, expected):
    from memebot.execution.live import custom_program_error_code

    assert custom_program_error_code(text) == expected


def test_the_exit_plan_asks_for_a_different_route_after_the_first_refusal(config):
    """Widening and splitting do nothing for a route the program refuses."""
    from memebot.engine import TradingEngine
    from memebot.storage import Storage
    from conftest import FakeDexScreener

    engine = TradingEngine(config, storage=Storage(":memory:"), data=FakeDexScreener([]))
    plan = engine._exit_plan(None)

    assert plan[0][2] is False, "the first attempt uses the best route available"
    assert any(direct for _, _, direct in plan[1:]), "a later one asks for a direct route"


def test_a_direct_route_request_reaches_jupiter(config):
    """The order carries it, or the fallback is the same attempt twice."""
    from memebot.execution.live import LiveExecutor
    from memebot.models import Order, Side, Token

    asked = []

    class Jupiter:
        def quote(self, in_mint, out_mint, amount, slippage_bps,
                  only_direct_routes=False, max_accounts=None):
            asked.append((only_direct_routes, max_accounts))
            return None

        def lookup_decimals(self, mint):
            return 6

        def to_base_units(self, mint, amount):
            return 1_000_000

    class Rpc:
        def get_token_balance(self, owner, mint):
            return 1_000.0

    executor = LiveExecutor.__new__(LiveExecutor)
    executor.cfg = config.execution
    executor.config = config
    executor.jupiter = Jupiter()
    executor.rpc = Rpc()
    executor._pubkey = "Wallet111"
    executor._ensure_wallet = lambda: None
    executor._quote_price_usd = lambda: 150.0

    order = Order(token=Token(address="mint-1", symbol="X"), side=Side.SELL,
                  reference_price=1.0, token_amount=10.0,
                  only_direct_routes=True, max_accounts=32)
    fill = executor._execute(order)

    assert asked == [(True, 32)]
    assert "no direct route found" in fill.error
