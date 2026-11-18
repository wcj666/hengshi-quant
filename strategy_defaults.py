"""Shared initialization parameters for V1 research and simulations."""

V1_VERSION = '1.0'
V1_EXIT_MODE = 'price_first'
V1_DEFAULTS = {
    'per_trade_risk': 150.0,
    'min_fees': 20.0,
    'max_position_pct': 15.0,
    'max_sector_pct': 30.0,
    'max_exposure_pct': 60.0,
    'max_positions': 4,
    'max_daily_opens': 1,
    'loss_budget': 1000.0,
    'pause_drawdown': 500.0,
    'reduce_drawdown': 800.0,
    'min_reward_risk': 1.5,
}

T_DEFAULTS = {
    'enabled': True,
    'mode': 'sell_first',
    'fee_reserve': V1_DEFAULTS['min_fees'],
    'max_fraction': 1 / 3,
    'max_cycles_per_code': 1,
    'last_open_time': '14:45',
    'stock_deviation_pct': .8,
    'etf_deviation_pct': .4,
}

SIMULATED_COSTS = {
    'commission_rate': .0005,
    'minimum_commission': 5.0,
    'stock_sell_stamp_rate': .0005,
    'stock_transfer_rate': .00001,
    'slippage_rate': .0005,
}
# The simulated order book and the minute replay apply this rate to different data.
SIM_PARTICIPATION = .10

BACKTEST_ENGINE_FILES = (
    'v1_backtest.py', 'v1_backtest_data.py', 'v1_backtest_report.py',
    'strategy_setup.py', 'strategy_v1.py', 'strategy_defaults.py', 'risk_engine.py',
    'trade_levels.py', 'quote_health.py', 'pnl_engine.py', 'v1_execution.py', 't_costs.py', 'replay_parameters.py',
)


def initial_v1_rules():
    """Create a new price-first ruleset without an invented account start time."""
    return {
        'enabled': True,
        'version': V1_VERSION,
        'exit_mode': V1_EXIT_MODE,
        **V1_DEFAULTS,
        'fee_reserve': V1_DEFAULTS['min_fees'],
        'account_risk_budget': V1_DEFAULTS['loss_budget'],
    }
