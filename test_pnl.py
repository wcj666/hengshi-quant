"""No-network tests for two new account P&L panels and conservative missing data."""
import copy
import sys
import types
from pathlib import Path
from app import snapshot_akshare, portfolio
from portfolio_test_fixture import account_fixture
from risk_engine import compute
from pnl_engine import calculate

cfg=account_fixture()
prices={p['code']:p['baseline_price'] for p in cfg['positions']}
prior={p['code']:p['baseline_price'] for p in cfg['positions']}
r=calculate(cfg,prices,prior,valid=True)
assert r['floating_pnl']==-1215.30, r
assert r['daily_pnl']==0.00 and r['daily_pct']==0.00
assert r['floating_pct'] is not None
assert r['positions']['300274']['floating_pnl']==-1109.10
assert r['positions']['123290']['floating_pnl']==325

# If one product lacks previous close, daily P&L must not silently be partial.
invalid=calculate(cfg,prices,{k:v for k,v in prior.items() if k!='123290'},valid=True)
assert invalid['valid'] and invalid['floating_pnl']==-1215.30
assert invalid['daily_pnl'] is None and '123290' in invalid['missing_previous']
assert calculate(cfg,prices,prior,valid=False)['floating_pnl'] is None
r=compute(cfg, prices, fresh=True, previous_closes=prior)
assert r['pnl']['floating_pnl']==-1215.30 and r['pnl']['daily_pnl']==0
assert r['positions'][0]['floating_pnl']==-1109.10
r=compute(cfg, {**prices,'300274':79.97}, fresh=True, previous_closes=prior)
assert r['pnl']['daily_pnl']==-600.00
assert r['pnl']['floating_pnl']==-1815.30

# Broker-native P&L can differ due to fees, cost precision, trading and distributions.
# Fake the AKShare module to verify prev_close field mapping without network.
class Frame:
    def __init__(self,row): self.cols=set(row);self.row=row
    @property
    def columns(self): return list(self.cols)
    def __contains__(self,key):return key in self.cols
    def iterrows(self):yield 0,self.row
class API:
    @staticmethod
    def stock_zh_a_spot_em():return Frame({'代码':'300274','名称':'阳光电源','最新价':81.97,'昨收':85.14})
    @staticmethod
    def fund_etf_spot_em():return Frame({'代码':'159929','名称':'医药ETF','最新价':1.348,'昨收':1.362})
    @staticmethod
    def bond_zh_hs_cov_spot():return Frame({'code':'123290','name':'申菱转债','trade':132.5,'pricechange':2.5})
sys.modules['akshare']=API
q,errors=snapshot_akshare()
assert not errors,errors
assert q[('stock','300274')][2]==85.14
assert q[('etf','159929')][2]==1.362
assert q[('convertible','123290')][2]==130.0
# First output still compatible with earlier price/name tuple indexing.
assert q[('stock','300274')][:2]==(81.97,'阳光电源')

# Insufficient source fields never fabricate a previous close.
API.bond_zh_hs_cov_spot=staticmethod(lambda:Frame({'code':'123290','name':'申菱转债','trade':132.5}))
q,_=snapshot_akshare()
assert q[('convertible','123290')][2] is None

print('PASS: 浮动盈亏/当日参考盈亏/昨收缺失处理/快照字段映射/代码兼容')
