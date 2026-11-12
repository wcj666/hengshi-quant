"""Synthetic account fixture, independent of the user's editable local ledger."""
from copy import deepcopy


_ACCOUNT = {
    'baseline_date': '2026-11-06',
    'baseline_equity': 60000.0,
    'max_additional_loss': 1000.0,
    'cash': 5891.40,
    'positions': [
        {'code': '300274', 'name': '阳光电源', 'type': 'stock',
         'quantity': 300, 'cost': 85.667, 'baseline_price': 81.97},
        {'code': '159929', 'name': '医药ETF', 'type': 'etf',
         'quantity': 12100, 'cost': 1.355, 'baseline_price': 1.348},
        {'code': '159562', 'name': '黄金股ETF', 'type': 'etf',
         'quantity': 6300, 'cost': 1.941, 'baseline_price': 1.886},
        {'code': '123290', 'name': '申菱转债', 'type': 'convertible',
         'quantity': 10, 'cost': 100, 'baseline_price': 132.5},
    ],
    'watchlist': [
        {'code': '600406', 'name': '国电南瑞'},
        {'code': '000682', 'name': '东方电子'},
        {'code': '002714', 'name': '牧原股份'},
    ],
    'alert_levels': [
        {'code': '300274', 'name': '阳光电源', 'below': 80, 'above': 85.667},
        {'code': '600406', 'name': '国电南瑞', 'below': 23, 'above': 24},
        {'code': '002714', 'name': '牧原股份', 'below': 42.68, 'above': 45},
        {'code': '000682', 'name': '东方电子', 'below': 9.5, 'above': 10.5},
    ],
    'risk_settings': {
        'stop_stock': 5.0, 'stop_etf': 4.0, 'stop_convertible': 10.0,
        'per_trade_risk': 150.0, 'max_position_pct': 15.0,
        'tp1_pct': 10.0, 'tp2_pct': 15.0,
    },
    'trade_plans': {},
}


def account_fixture():
    return deepcopy(_ACCOUNT)
