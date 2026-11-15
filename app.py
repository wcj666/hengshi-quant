"""A股 A+B: 观察名单量价扫描 + 账户回撤监控（不交易）。"""
from __future__ import annotations
import argparse
from datetime import datetime, date
import html
import json
import math
from pathlib import Path
import sys
import time

import pandas as pd

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'output'
COLUMNS = ['日期','股票代码','股票名称','收盘','涨跌幅%','MA5','MA20','20日突破','量比5日','观察评分','说明']


def load_config(path):
    cfg=json.loads(Path(path).read_text(encoding='utf-8'))
    assert cfg['max_additional_loss'] > 0
    for p in cfg['positions']:
        assert p['type'] in ('stock','etf','convertible')
        assert p['quantity']>=0 and p['baseline_price']>0
    # 允许交易后更新持仓和现金。固定基准净值作为回撤比较的账户锚点；
    # 不能再强制现有持仓按旧基准价加总等于历史净值。
    if cfg['baseline_equity'] <= 0 or cfg['cash'] < 0:
        raise ValueError('账户基准必须为正、现金不能为负')
    return cfg


def code_str(x):
    return str(x).zfill(6)


def snapshot_akshare(cfg=None):
    if cfg is not None:
        from portfolio_quotes import snapshot_selected_tencent
        return snapshot_selected_tencent(cfg, include_watch=True, deadline_seconds=7.5)
    import akshare as ak
    result={}
    errors=[]
    feeds=[('stock',ak.stock_zh_a_spot_em,'代码','名称','最新价'),
           ('etf',ak.fund_etf_spot_em,'代码','名称','最新价'),
           ('convertible',ak.bond_zh_hs_cov_spot,'code','name','trade')]
    for typ, fn, codecol, namecol, pricecol in feeds:
        try:
            df=fn()
            if codecol not in df or pricecol not in df:
                raise ValueError('字段变化: '+','.join(map(str,df.columns)))
            for _,r in df.iterrows():
                try:
                    px=float(r[pricecol])
                    if px>0:
                        # A 3rd tuple element is optional previous-close metadata.
                        # Existing callers indexing [0] and [1] remain compatible.
                        from pnl_engine import valid_price
                        prev=valid_price(r.get('昨收'))
                        if typ=='convertible':
                            # Sina convertible quotes publish pricechange as an absolute
                            # difference; never treat changepercent as a price change.
                            delta=r.get('pricechange')
                            try:
                                delta=float(delta)
                                if math.isfinite(delta):
                                    prev=valid_price(px-delta)
                            except (ValueError, TypeError):
                                pass
                        result[(typ,code_str(r[codecol]))]=(px,str(r.get(namecol,'')),prev)
                except (ValueError,TypeError):
                    pass
        except Exception as e:
            errors.append(f'{typ} 行情失败: {type(e).__name__}: {e}')
    # 数据源不保证统一时间戳：只作快照，不作为有交易所时间戳的实时行情。
    return result,errors


def stock_history(code, begin, end=None):
    end=end or date.today().strftime('%Y%m%d')
    try:
        from asset_history import tencent_history
        return tencent_history(code,'stock',begin,end)
    except Exception:
        return eastmoney_stock_history(code,begin,end)


def eastmoney_stock_history(code,begin,end=None):
    import akshare as ak
    df=ak.stock_zh_a_hist(symbol=code, period='daily', start_date=begin,
                          end_date=end or date.today().strftime('%Y%m%d'), adjust='qfq')
    cols={'日期':'date','开盘':'open','收盘':'close','最高':'high','最低':'low','成交量':'volume'}
    if df.empty or any(k not in df for k in ('日期','收盘','最高','最低','成交量')):
        raise ValueError('日线为空或缺少真实最高/最低价字段')
    df=df.rename(columns=cols)
    df['date']=pd.to_datetime(df['date'],errors='coerce')
    for x in ('open','close','high','low','volume'):
        if x in df:
            df[x]=pd.to_numeric(df[x],errors='coerce')
    df=df.dropna(subset=['date','close','high','low','volume']).sort_values('date')
    df.attrs.update(source='东方财富日K',adjust='qfq')
    return df


def score_stock(code,name,df):
    if len(df)<22: raise ValueError('日线不足22根，不能评分')
    p=df.iloc[-1]
    previous=df.iloc[-2]
    ma5=df.close.tail(5).mean()
    ma20=df.close.tail(20).mean()
    vol5=df.volume.iloc[-6:-1].mean()
    ratio=(float(p.volume)/vol5) if vol5>0 else float('nan')
    high20=float(df.high.iloc[-21:-1].max())
    breakout=bool(p.close>high20)
    change=(float(p.close)/float(previous.close)-1)*100
    score=int(p.close>ma5)*25+int(ma5>ma20)*25+int(breakout)*30+int(ratio>=1.3)*20
    desc=[]
    if breakout:desc.append('突破前20日高点')
    if ratio>=1.3:desc.append('成交量较前5日均量放大')
    if ma5<=ma20:desc.append('短均线未占优')
    if not desc:desc.append('观察，未满足强势条件')
    return {'日期':str(p.date.date()),'股票代码':code,'股票名称':name,
            '收盘':round(float(p.close),3),'涨跌幅%':round(change,2),
            'MA5':round(ma5,3),'MA20':round(ma20,3),
            '20日突破':'是' if breakout else '否',
            '量比5日':round(ratio,2),'观察评分':score,'说明':'；'.join(desc)}


def scan(cfg, mode):
    rows=[]; errors=[]
    # 首次使用只扫描观察池，避免几千只股票同时抓取造成限流。
    begin=(pd.Timestamp.today()-pd.Timedelta(days=100)).strftime('%Y%m%d')
    for s in cfg['watchlist']:
        code=code_str(s['code'])
        try:
            if mode=='demo':
                # 仅用于测试评分/报表，合成数据绝不作为实际行情。
                import numpy as np
                rng=np.random.default_rng(int(code))
                close=10+pd.Series(rng.normal(.05,.2,50)).cumsum()
                hist=pd.DataFrame({'date':pd.bdate_range(end=pd.Timestamp.today().normalize(),periods=50),
                                   'close':close,'high':close+0.3,
                                   'volume':rng.integers(80000,200000,50)})
            else:
                hist=stock_history(code,begin)
            rows.append(score_stock(code,s['name'],hist))
        except Exception as e:
            errors.append(f"{code} {s['name']}: {type(e).__name__}: {e}")
        if mode=='live':time.sleep(0.25)
    df=pd.DataFrame(rows,columns=COLUMNS)
    if not df.empty: df=df.sort_values(['观察评分','涨跌幅%'],ascending=False)
    return df,errors


def portfolio(cfg,quotes,mode):
    rows=[];missing=[]
    for p in cfg['positions']:
        code=code_str(p['code'])
        item=quotes.get((p['type'],code))
        if mode=='demo':
            # 演示只使用用户基准价；非当前行情。
            px=p['baseline_price']
            status='演示基准价'
        elif item:
            px=item[0]
            status='抓取快照（时间未核验）'
        else:
            px=None
            status='缺失，不用旧价冒充实时价'
            missing.append(f"{p['name']}({code})")
        from pnl_engine import valid_price
        prev=valid_price(item[2]) if mode!='demo' and item and len(item)>2 else None
        floating=round((px-p['cost'])*p['quantity'],2) if px is not None else None
        daily=round((px-prev)*p['quantity'],2) if px is not None and prev is not None else None
        value=round(px*p['quantity'],2) if px is not None else None
        delta=round((px-p['baseline_price'])*p['quantity'],2) if px is not None else None
        rows.append({'证券':p['name'],'代码':code,'类型':p['type'],
                     '数量':p['quantity'],'基准价':p['baseline_price'],
                     '当前快照价':px,'当前市值':value,
                     '昨收参考':prev,'浮动盈亏估算':floating,'当日参考盈亏估算':daily,
                     '相对11/6变动':delta,'状态':status})
    df=pd.DataFrame(rows)
    if missing:
        equity=None; loss=None;remain=None
    else:
        equity=round(float(df['当前市值'].sum())+cfg['cash'],2)
        loss=round(cfg['baseline_equity']-equity,2)
        remain=round(cfg['max_additional_loss']-max(0,loss),2)
    return df,missing,equity,loss,remain


def scenarios(cfg):
    data=[]
    for scenario in cfg['pressure_scenarios']:
        total=0
        for p in cfg['positions']:
            total+=p['baseline_price']*p['quantity']*scenario[p['type']]
        data.append({'情景':scenario['name'],'额外变动(元)':round(total,2),
                     '超过1000元预算':'是' if -total>cfg['max_additional_loss'] else '否'})
    return pd.DataFrame(data)


def save_report(cfg,scan_df,port_df,stress,errors,missing,equity,loss,remain,mode):
    OUT.mkdir(exist_ok=True)
    scan_df.to_csv(OUT/'scan.csv',index=False,encoding='utf-8-sig')
    port_df.to_csv(OUT/'portfolio.csv',index=False,encoding='utf-8-sig')
    stress.to_csv(OUT/'stress.csv',index=False,encoding='utf-8-sig')
    timestamp=datetime.now().astimezone().isoformat(timespec='seconds')
    status='⚠️ 演示数据（非真实行情）' if mode=='demo' else '行情抓取快照（源数据可能延迟）'
    baseline=f"{cfg['baseline_date']} / ¥{cfg['baseline_equity']:,.2f}"
    def table(df):
        return df.to_html(index=False,escape=True,classes='data',na_rep='—',border=0)
    if missing:
        summary='部分持仓行情缺失，禁止计算“当前账户剩余风险预算”：'+ '、'.join(missing)
    elif mode=='demo':
        summary=f'演示账户净值 ¥{equity:,.2f}；演示额外损失 ¥{loss:,.2f}；仅用于测试，不得作为交易依据。'
    else:
        summary=f'快照估算净值 ¥{equity:,.2f}；相对基准变动 ¥{-loss:,.2f}；预算余量 ¥{remain:,.2f}（不保证未来损失封顶）。'
    note='；'.join(errors) if errors else '无抓取报错（不代表行情无延迟）。'
    # 编写一个无需服务器的离线 HTML 报告。
    content=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>A股A+B量化观察报告</title><style>body{{font-family:system-ui,-apple-system,sans-serif;max-width:1120px;margin:35px auto;padding:0 18px;color:#243047;background:#f7f9fc}}h1{{font-size:27px}}h2{{margin-top:36px}}.panel{{background:white;padding:20px;border-radius:14px;box-shadow:0 2px 12px #ddd7}}.data{{border-collapse:collapse;width:100%;background:white;font-size:13px}}th,td{{padding:10px;border-bottom:1px solid #e9edf4;text-align:left}}th{{background:#e9eef9;position:sticky;top:0}}.scroll{{overflow-x:auto}}.warning{{color:#a3480b}}small{{color:#697386}}code{{background:#eef2f7;padding:3px 5px;border-radius:4px}}</style>
<h1>A股量化选股 + 持仓风险报告</h1><div class="panel"><strong>{html.escape(status)}</strong><p>生成时间：{html.escape(timestamp)}<br>基准：{html.escape(baseline)}<br>最大额外亏损预算：¥{cfg['max_additional_loss']:,.2f}</p><p class="warning">{html.escape(summary)}</p><small>禁止直接依据本报告自动下单。成交滑点/跳空不能被本系统控制。</small></div>
<h2>A：观察池量价候选（非买入推荐）</h2><div class="scroll">{table(scan_df)}</div>
<p><small>评分：收盘高于MA5（25分）+MA5高于MA20（25分）+突破前20日高点（30分）+成交量≥前5日均量1.3倍（20分）。仅做排序，不构成统计回测收益。</small></p>
<h2>B：持仓市值与预算</h2><div class="scroll">{table(port_df)}</div><h2>组合压力测试（基准持仓）</h2><div class="scroll">{table(stress)}</div>
<h2>数据异常与限制</h2><p>{html.escape(note)}</p><p>股票日线使用前复权；ETF/可转债快照数据并不保证同一时刻；账户现金与持仓变动请手动更新config.json；不含交易费用、税费、分红、资金变动。</p>
</html>'''
    (OUT/'report.html').write_text(content,encoding='utf-8')
    return OUT/'report.html'


def main():
    parser=argparse.ArgumentParser(description='A股观察池+持仓风控，不自动交易')
    parser.add_argument('--mode',choices=['live','demo'],default='demo',help='demo 不联网；live 抓取网络行情')
    parser.add_argument('--config',default=str(ROOT/'config.json'))
    args=parser.parse_args()
    cfg=load_config(args.config)
    quotes={};errors=[]
    if args.mode=='live':
        quotes,errors=snapshot_akshare(cfg)
    scan_df,scan_errors=scan(cfg,args.mode)
    errors+=scan_errors
    port_df,missing,equity,loss,remain=portfolio(cfg,quotes,args.mode)
    stress=scenarios(cfg)
    report=save_report(cfg,scan_df,port_df,stress,errors,missing,equity,loss,remain,args.mode)
    print(f'已生成: {report}')
    print('A候选数:',len(scan_df),'| B持仓数据缺失:',len(missing))
    if errors:
        print('注意：',*errors,sep='\n - ')
    if missing:print('当前预算未计算：存在缺失行情，禁止用旧价填充。')

if __name__=='__main__':
    main()
