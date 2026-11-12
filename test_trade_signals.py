"""Offline tests: candidate sizing, high-risk reduction, switching and no-demo signals."""
import copy
from datetime import datetime, timezone
from portfolio_test_fixture import account_fixture
from risk_engine import compute
from trade_levels import make_levels
from trade_signals import build_signals

base=account_fixture()
cfg=copy.deepcopy(base)
cfg['baseline_equity']=8000.
cfg['cash']=6000.
cfg['max_additional_loss']=1000.
cfg['positions']=[{'code':'300274','name':'测试持仓','type':'stock','quantity':100,'cost':20.,'baseline_price':20.}]
cfg['watchlist']=[{'code':'600406','name':'测试候选'}]
cfg['trade_plans']={'600406':{'entry_price':24,'stop_loss':23,'take_profit_1':28,'take_profit_2':30}}
cfg['risk_settings']['max_position_pct']=40.
cfg['risk_settings']['per_trade_risk']=200.
cfg['alert_levels']=[{'code':'300274','name':'测试持仓','below':21},
                     {'code':'600406','name':'测试候选','above':24}]

def evaluate(c, prices):
    quotes={}
    for code,name,price,held in [('300274','测试持仓',prices[0],True),('600406','测试候选',prices[1],False)]:
        plan=make_levels(c,code,price=price,position=c['positions'][0] if held else None)
        quotes[code]={'code':code,'name':name,'price':price,'held':held,'asset_type':'stock','source_timestamp':datetime.now(timezone.utc).isoformat(),
                      'stop_loss':plan['stop_loss'],'take_profit_1':plan['take_profit_1'],
                      'take_profit_2':plan['take_profit_2']}
    risk=compute(c,{'300274':prices[0]},fresh=True,
                 watch_quotes=[{'code':'600406','name':'测试候选','price':prices[1]}])
    sig=build_signals(c,quotes,risk,trading_day='2026-11-09',market_session=True,calendar_ok=True)
    return risk,sig

r,s=evaluate(cfg,(20,24.5))
assert r['valid'] and r['available_new_risk']>0
assert r['sizing'][0]['max_buy_shares']>=100
assert {x['category'] for x in s}=={'reduce','add','switch'}
assert '先在券商确认卖出' in next(x for x in s if x['category']=='switch')['reason']
# A breached fixed stop shuts down add and switch.
stop=copy.deepcopy(cfg)
stop['trade_plans']={'300274':{'entry_price':23,'stop_loss':22,
                              'take_profit_1':25,'take_profit_2':27}}
r,s=evaluate(stop,(20,24.5))
assert r['stop_breaches'] and not any(x['category'] in ('add','switch') for x in s)
assert any(x['trigger']=='stop' for x in s)
# Incomplete portfolio data must block additional capacity.
r=compute(cfg,{},fresh=False,watch_quotes=[{'code':'600406','name':'测试候选','price':24.5}])
s=build_signals(cfg,{'600406':{'name':'测试候选','price':24.5}},r,trading_day='2026-11-09',market_session=True,calendar_ok=True)
assert not any(x['category']=='add' for x in s)
# No live prompts at auction, weekends, demo or invalid calendar.
_,s=evaluate(cfg,(20,24.5))
for args in [dict(market_session=False,calendar_ok=True),dict(market_session=True,calendar_ok=False),dict(market_session=True,calendar_ok=True,demo=True)]:
    assert build_signals(cfg,{'300274':{'name':'测试持仓','price':20}},r,trading_day='2026-11-09',**args)==[]
print('PASS: 减仓、加仓额度、换仓先卖后核算、缺失行情、竞价与演示安全门控')
