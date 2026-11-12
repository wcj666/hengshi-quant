"""Core self-test without network or broker login. Run: python self_test.py"""
from portfolio_test_fixture import account_fixture
from risk_engine import compute
from trade_levels import make_levels, validate_plan

cfg=account_fixture()
q={str(x['code']).zfill(6):x['baseline_price'] for x in cfg['positions']}
w=[{'code':'600406','name':'国电南瑞','price':23.86}]
r=compute(cfg,q,fresh=True,watch_quotes=w)
assert r['valid'] and round(r['equity'],2)==60000.0
assert r['remaining']==1000
assert r['available_new_risk']==0, '现有持仓压力测试超过预算，不应计算可新增股数'
assert r['sizing'][0]['max_buy_shares']==0
l=make_levels(cfg,'600406',price=23.86)
assert l['stop_loss'] < l['reference'] < l['take_profit_1'] < l['take_profit_2']
try:
    validate_plan({'entry_price':10,'stop_loss':11,'take_profit_1':12,'take_profit_2':14})
    raise AssertionError('应拒绝错误价格顺序')
except ValueError: pass
cfg['trade_plans']={'300274': {'entry_price':85.667,'stop_loss':82,'take_profit_1':94,'take_profit_2':99}}
r=compute(cfg,q,fresh=True,watch_quotes=w)
assert '300274' in r['stop_breaches'] and r['sizing'][0]['max_buy_shares']==0
r=compute(cfg,{},fresh=False,watch_quotes=w)
assert not r['valid'] and r['sizing'][0]['max_buy_shares'] is None
print('PASS: 固定止盈止损 / 百分比默认 / 风控预算 / 触发止损禁新增 / 缺失行情不计算')
