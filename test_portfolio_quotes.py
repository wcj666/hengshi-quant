"""Offline adapter tests; synthetic fixtures only, no live network requests."""
from pathlib import Path
from unittest.mock import patch, Mock
import requests
from portfolio_quotes import _etf_quote
import pandas as pd
from portfolio_quotes import snapshot_selected_akshare

class FakeAK:
    def __init__(self): self.stocks=[];self.etfs=0;self.bonds=0
    def stock_bid_ask_em(self, symbol):
        self.stocks.append(symbol)
        if symbol=='300274':
            return pd.DataFrame({'item':['最新','昨收','涨跌'],'value':[81.50,82.00,-.50]})
        raise ValueError('模拟盘口失败')
    def fund_etf_spot_em(self):
        self.etfs+=1
        return pd.DataFrame({'代码':['159929','159562','510300'], '名称':['医药ETF','黄金股ETF','其他ETF'],
                             '最新价':[1.350,1.880,4.1], '昨收':[1.346,1.890,4.0]})
    def bond_zh_hs_cov_spot(self):
        self.bonds+=1
        return pd.DataFrame({'code':['123290','113000'], 'name':['申菱转债','其他转债'],
                             'trade':[131.50,125.0], 'pricechange':[-1,1]})

cfg={'positions':[
    {'type':'stock','code':'300274','quantity':300},
    {'type':'etf','code':'159929','quantity':12100},
    {'type':'etf','code':'159562','quantity':6300},
    {'type':'convertible','code':'123290','quantity':10},
  ],'watchlist':[{'code':'300274'},{'code':'000682'}]}
provider=FakeAK()
def etf_response(url, *, params, timeout):
    code=params['secid'].split('.')[1]
    values={'159929':(1.350,1.346),'159562':(1.880,1.890),'510300':(4.1,4.0)}
    price,previous=values[code]
    assert params['secid']==('1.' if code.startswith('5') else '0.')+code
    assert timeout==(2,4)
    return Mock(json=lambda:{'data':{'f57':code,'f43':price,'f60':previous,'f58':'测试ETF'}})
with patch('portfolio_quotes.requests.get',side_effect=etf_response):
    quotes,errors=snapshot_selected_akshare(cfg,ak=provider)
    assert _etf_quote(provider,'510300')[0]==4.1
for bad in [{'data':None},{'data':{'f57':'159562','f43':1.2}},
            {'data':{'f57':'159929','f43':'-'}}]:
    with patch('portfolio_quotes.requests.get',return_value=Mock(json=lambda:bad)):
        try:_etf_quote(provider,'159929')
        except ValueError:pass
        else:raise AssertionError('invalid ETF quote accepted')
with patch('portfolio_quotes.requests.get',side_effect=requests.Timeout('test timeout')):
    missing,failures=snapshot_selected_akshare({'positions':[{'type':'etf','code':'159929','quantity':100}]},ak=provider)
    assert not missing and any('159929' in e for e in failures)

assert set(quotes)=={('stock','300274'),('etf','159929'),('etf','159562'),('convertible','123290')}
assert quotes[('stock','300274')][0]==81.5
assert quotes[('stock','300274')][2]==82.0
assert quotes[('etf','159929')][2]==1.346
assert quotes[('convertible','123290')][2]==132.5
assert provider.etfs==0 and provider.bonds==1
assert sorted(provider.stocks)==['000682','300274']
assert any('000682' in e for e in errors)

root=Path(__file__).resolve().parent
html=(root/'templates/dashboard.html').read_text()
import re
assert re.findall(r'<a href="#([^"]+)" class="nav-link', html)[:3]==['overview','risk','watch']
assert re.findall(r'<section id="([^"]+)"',html)[:3]==['overview','risk','intraday']
assert '<span class="version">v3.6.1</span>' in html
assert (root/'VERSION.txt').read_text().strip()=='3.6.1'
assert "'--interval', '10', '--notify'" in (root/'setup_autostart.py').read_text()
print('PASS: simulated four-asset quote parsing; missing quote not reused; navigation and 10s agent settings')
