"""No-network tests: test session gating, stop-loss and portfolio budget math."""
from datetime import datetime
from zoneinfo import ZoneInfo
from portfolio_test_fixture import account_fixture
from intraday_monitor import in_session,evaluate,example_quotes,now_cn

cst=ZoneInfo('Asia/Shanghai')
assert in_session(datetime(2026,11,9,9,45,tzinfo=cst))
assert not in_session(datetime(2026,11,9,12,5,tzinfo=cst))
assert in_session(datetime(2026,11,9,14,58,tzinfo=cst))
assert not in_session(datetime(2026,11,9,15,1,tzinfo=cst))
assert not in_session(datetime(2026,11,8,9,50,tzinfo=cst))
cfg=account_fixture()
quotes=example_quotes(cfg)[0]
q,e,r=evaluate(cfg,quotes,demo=True)
assert r['valid'] and r['equity']==60000.0
assert all(not (x['kind'].startswith('触及止') and x['code'] not in {p['code'] for p in cfg['positions']}) for x in e)
# Trigger held-stock stop and approximate portfolio risk warnings.
quotes[('stock','300274')]=(76.0,'阳光电源')
q,e,r=evaluate(cfg,quotes,demo=True)
assert any(x['kind']=='触及止损参考' and x['code']=='300274' for x in e)
assert any('回撤预算使用100%预警'==x['kind'] for x in e)
# Missing convertible bond must disable full-account budget estimate.
del quotes[('convertible','123290')]
q,e,r=evaluate(cfg,quotes,demo=True)
assert not r['valid'] and r['equity'] is None
assert not any(x['code']=='PORT' for x in e)
print('PASS: 交易时段门控、持仓止损、账户预算、缺失行情禁止完整估算')
