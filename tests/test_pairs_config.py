"""
Tests for config-driven pairs parameters (B12).

strategy.conf's [pairs] section drives PairConfig; v15 values are the
fallbacks so a missing/broken conf can never silently change behavior.
"""

import pytest

import config.live_config as live_config
from config.live_config import PAIRS_PARAM_DEFAULTS, load_pairs_params
from execution.market_spec import make_market_spec
from execution.pair_runner import PairRunner


def _write_conf(tmp_path, body: str) -> str:
    path = tmp_path / "strategy.conf"
    path.write_text(body)
    return str(path)


def test_missing_file_returns_v15_defaults(tmp_path):
    params = load_pairs_params(str(tmp_path / "nope.conf"))
    assert params == PAIRS_PARAM_DEFAULTS
    assert params["max_pair_cost"] == 0.96       # v15, not dataclass 0.99
    assert params["max_skew_pct"] == 0.30        # v15, not dataclass 0.50


def test_missing_section_returns_defaults(tmp_path):
    path = _write_conf(tmp_path, "[strategy]\nmode = paper\n")
    assert load_pairs_params(path) == PAIRS_PARAM_DEFAULTS


def test_overrides_are_read(tmp_path):
    path = _write_conf(tmp_path, (
        "[pairs]\n"
        "max_pair_cost = 0.94\n"
        "buy_size_usd = 25\n"
        "max_book_walk_levels = 5\n"
    ))
    params = load_pairs_params(path)
    assert params["max_pair_cost"] == pytest.approx(0.94)
    assert params["buy_size_usd"] == pytest.approx(25.0)
    assert params["max_book_walk_levels"] == 5    # int stays int
    # untouched keys keep defaults
    assert params["sniper_threshold"] == pytest.approx(0.35)


def test_one_bad_value_does_not_discard_the_rest(tmp_path):
    path = _write_conf(tmp_path, (
        "[pairs]\n"
        "max_pair_cost = not-a-number\n"
        "buy_size_usd = 25\n"
    ))
    params = load_pairs_params(path)
    assert params["max_pair_cost"] == pytest.approx(0.96)   # fell back
    assert params["buy_size_usd"] == pytest.approx(25.0)    # survived


def test_repo_conf_has_no_unknown_keys():
    # Every key in the checked-in [pairs] section must be a real param —
    # a typo'd key silently does nothing, which would corrupt an
    # experiment's interpretation. (Values are free to change per
    # experiment; only the KEY NAMES are pinned.)
    import configparser
    cp = configparser.ConfigParser()
    cp.read("config/strategy.conf")
    unknown = [k for k in cp.options("pairs") if k not in PAIRS_PARAM_DEFAULTS]
    assert unknown == [], f"typo'd [pairs] keys with no effect: {unknown}"


def test_unknown_key_warns_but_still_loads(tmp_path, caplog):
    import logging
    path = _write_conf(tmp_path, (
        "[pairs]\n"
        "max_pair_costt = 0.91\n"      # typo — must warn, not crash
        "buy_size_usd = 25\n"
    ))
    with caplog.at_level(logging.WARNING):
        params = load_pairs_params(path)
    assert params["buy_size_usd"] == pytest.approx(25.0)
    assert params["max_pair_cost"] == pytest.approx(0.96)   # typo had no effect
    assert any("max_pair_costt" in r.message for r in caplog.records)


def test_runner_builds_engine_from_conf(tmp_path, monkeypatch):
    path = _write_conf(tmp_path, (
        "[pairs]\n"
        "max_pair_cost = 0.93\n"
        "max_unmatched_usd = 12\n"
    ))
    monkeypatch.setattr(live_config, "CONFIG_PATH", path)

    runner = PairRunner(mode="paper", spec=make_market_spec("btc", "5m"))
    assert runner.engine.config.max_pair_cost == pytest.approx(0.93)
    assert runner.engine.config.max_unmatched_usd == pytest.approx(12.0)
    # timing still comes from the spec, not the conf
    assert runner.engine.config.panic_time_seconds == runner.spec.panic_time_seconds


def test_apply_per_window_picks_up_conf_edit_and_stamps_csv(tmp_path, monkeypatch):
    import execution.pair_logger as pair_logger

    path = _write_conf(tmp_path, "[pairs]\nmax_pair_cost = 0.96\n")
    monkeypatch.setattr(live_config, "CONFIG_PATH", path)

    runner = PairRunner(mode="paper", spec=make_market_spec("btc", "5m"))
    assert runner.engine.config.max_pair_cost == pytest.approx(0.96)

    # simulate an experiment edit between windows
    _write_conf(tmp_path, "[pairs]\nmax_pair_cost = 0.91\n")
    runner._apply_pairs_config("BTC Up/Down 5m — test-window")

    assert runner.engine.config.max_pair_cost == pytest.approx(0.91)

    # the active set was stamped into the sidecar CSV (hermetic LOG_DIR)
    import glob
    files = glob.glob(f"{pair_logger.LOG_DIR}/pair_params_*.csv")
    assert files, "pair_params CSV not written"
    content = open(files[0]).read()
    assert "0.91" in content and "test-window" in content
