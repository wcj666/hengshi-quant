"""A股全市场快照初筛、板块强度、信号报警；非自动下单。
公开行情接口可能延迟或变更；不保证实时。"""
from __future__ import annotations
import argparse, json, html, sys, time, hashlib
from datetime import datetime
from pathlib import Path
import pandas as pd
import numpy as np

from app import ROOT, OUT, load_config, code_str, stock_history, score_stock, snapshot_akshare, portfolio, scenarios
from trade_levels import make_levels
from chan_analysis import analyze_chan
from daily_observe import select_five
from strategy_v1 import enabled as strategy_enabled

FIELDS = {'代码':'code','名称':'name','最新价':'price','涨跌幅':'pct','成交额':'amount','换手率':'turnover','量比':'volume_ratio', '总市值':'market_cap'}
SECTOR_FIELDS={'板块名称':'name','涨跌幅':'pct','上涨家数':'up','下跌家数':'down','领涨股票':'leader','领涨股票-涨跌幅':'leader_pct','总市值':'market_cap'}


def full_snapshot(mode):
    if mode=='demo':
        rng=np.random.default_rng(20261106)
        names=['国电南瑞','东方电子','中国西电','牧原股份','阳光电源','润建股份','公元股份','东方嘉盛']
        data=[]
        for i in range(1000):
            code = ['600406','000682','601179','002714','300274','002929','002641','002889'][i] if i<8 else (f'{600000+i:06d}' if i%2 else f'{200000+i:06d}')
            data.append({'code':code,'name':names[i] if i<8 else f'演示股票{i:04}', 'price':round(float(rng.uniform(3,100)),2),'pct':round(float(rng.normal(1.2,3)),2),'amount':round(float(rng.uniform(0.5,40)*1e8),0),'turnover':round(float(rng.uniform(0.5,15)),2),'volume_ratio':round(float(rng.uniform(.5,3)),2),'market_cap':round(float(rng.uniform(15,1500)*1e8),0)})
        return pd.DataFrame(data),'演示合成数据'
    import akshare as ak
    raw=ak.stock_zh_a_spot_em()
    required=['代码','名称','最新价','涨跌幅','成交额','换手率','量比']
    missing=[x for x in required if x not in raw.columns]
    if missing: raise RuntimeError('全市场快照接口字段缺失: '+','.join(missing))
    frame=raw.rename(columns=FIELDS).copy()
    for key in ('price','pct','amount','turnover','volume_ratio','market_cap'):
        if key in frame:frame[key]=pd.to_numeric(frame[key], errors='coerce')
    frame['code']=frame['code'].astype(str).str.zfill(6)
    return frame,'AKShare stock_zh_a_spot_em，时间戳未由交易所核验'


def sector_snapshot(mode):
    if mode=='demo':
        rng=np.random.default_rng(7)
        data=[]
        for name in ['电网设备','通信设备','物流行业','软件开发','食品饮料','农牧饲渔','医药商业','贵金属','光伏设备','水利建设','半导体','汽车零部件']:
            up=int(rng.integers(5,55)); down=int(rng.integers(4,45))
            data.append({'name':name,'pct':round(float(rng.normal(1,2)),2),'up':up,'down':down,'leader':'演示龙头','leader_pct':round(float(rng.uniform(1,9)),2)})
        return pd.DataFrame(data),'演示合成数据'
    import akshare as ak
    raw=ak.stock_board_industry_name_em()
    required=['板块名称','涨跌幅']
    missing=[x for x in required if x not in raw.columns]
    if missing:raise RuntimeError('行业接口字段缺失: '+','.join(missing))
    out=raw.rename(columns=SECTOR_FIELDS)
    for key in ['pct','up','down','leader_pct']:
        if key in out: out[key]=pd.to_numeric(out[key],errors='coerce')
    return out,'AKShare stock_board_industry_name_em，行业分类来自数据源'


def filter_universe(df, top_n,cfg=None):
    q=df.copy()
    if strategy_enabled(cfg or {}):
        q=q[q.code.str.match(r'^(60\d{4}|00\d{4})$')]
        q=q[~q.name.astype(str).str.contains(r'ST|退',case=False,regex=True)]
        q=q[(q.price>0)&(q.amount>0)]
        # Snapshot liquidity only orders the limited history fetch queue. Entry
        # qualification comes exclusively from complete daily V1 setup checks.
        q['初筛分']=0.
        return q.sort_values('amount',ascending=False).head(top_n)
    # 排除北交所、科创板、B股，保留沪深主板+创业板；排除ST、退市及无效数据。
    q=q[q.code.str.match(r'^(60\d{4}|00\d{4}|30\d{4})$')]
    q=q[~q.name.astype(str).str.contains(r'ST|退',case=False,regex=True)]
    q=q[(q.price>0)&q.pct.between(-20,20)&(q.amount>=1e8)&(q.turnover>=1)&(q.volume_ratio>=1.1)]
    q['初筛分']= (q.pct.clip(lower=0,upper=10)*3 + q.turnover.clip(upper=20)*1.4 + q.volume_ratio.clip(upper=4)*12 + np.log1p(q.amount/1e8)*4).round(2)
    return q.sort_values(['初筛分','amount'],ascending=False).head(top_n)


def v1_benchmark_history(mode):
    """One benchmark lookup per scan, with a completed-bar cache fallback."""
    from zoneinfo import ZoneInfo
    from strategy_setup import completed_history
    now=datetime.now(ZoneInfo('Asia/Shanghai'));path=OUT/'strategy_evidence'/'benchmark.json'
    cached=pd.DataFrame();stamp=None;fetched=None
    try:
        payload=json.loads(path.read_text(encoding='utf-8'))
        cached,error=completed_history(pd.DataFrame(payload.get('history',[])),now)
        if error:cached=pd.DataFrame()
        stamp=payload.get('fetched_at','')[:10]
        fetched=datetime.fromisoformat(payload.get('fetched_at',''))
    except (OSError,ValueError,TypeError):pass
    if mode!='live':return pd.DataFrame(),'V1演示模式不生成真实形态候选'
    refreshed_after_close=bool(fetched and (now.hour*60+now.minute<905 or fetched.hour*60+fetched.minute>=905))
    if stamp==now.date().isoformat() and refreshed_after_close and cached is not None and len(cached)>=120:return cached,None
    try:
        import akshare as ak
        raw=ak.index_zh_a_hist(symbol='000300',period='daily',start_date=(pd.Timestamp(now)-pd.Timedelta(days=3*365+60)).strftime('%Y%m%d'),end_date=now.strftime('%Y%m%d'))
        frame=raw.rename(columns={'日期':'date','开盘':'open','收盘':'close','最高':'high','最低':'low','成交量':'volume','成交额':'amount'})
        frame,error=completed_history(frame,now)
        if error or frame is None or len(frame)<120:raise ValueError(error or '完整沪深300日K不足120根')
        from strategy_runtime import write
        records=frame.copy();records['date']=records.date.map(str)
        write(path,{'history':json.loads(records.to_json(orient='records')),'fetched_at':now.isoformat()})
        return frame,None
    except Exception as exc:
        if cached is not None and len(cached)>=120:return cached,'沪深300更新失败，使用完整日线缓存：'+type(exc).__name__
        return pd.DataFrame(),'沪深300完整日线不可用：'+type(exc).__name__


def _verify_v1_candidates(candidates,mode,max_calls,pause,cfg,benchmark_hist=None):
    from strategy_setup import build_setup,completed_history
    rows=[];errors=[]
    if benchmark_hist is None:
        benchmark_hist,problem=v1_benchmark_history(mode)
        if problem:errors.append(problem)
    if mode!='live' or benchmark_hist is None or benchmark_hist.empty:
        if not errors:errors.append('V1缺少完整沪深300日线，不生成候选')
        return pd.DataFrame(),errors
    begin=(pd.Timestamp.today()-pd.Timedelta(days=3*365+60)).strftime('%Y%m%d')
    for _,snapshot in candidates.head(max_calls).iterrows():
        code=code_str(snapshot['code'])
        if not code.startswith(('00','60')):continue
        try:
            hist=stock_history(code,begin).rename(columns={'成交额':'amount'})
            setup=build_setup(hist,benchmark_hist,asset_type='stock',name=str(snapshot['name']))
            complete,error=completed_history(hist,amount=True)
            if error or complete is None or complete.empty:raise ValueError(error or '完整日线为空')
            latest=complete.iloc[-1];prior=complete.iloc[-2] if len(complete)>1 else latest
            levels=setup.get('levels',{})
            rows.append({'日期':str(latest.date),'股票代码':code,'股票名称':str(snapshot['name']),
                         '收盘':float(latest.close),'涨跌幅%':round((float(latest.close)/float(prior.close)-1)*100,2),
                         '初筛分':snapshot.get('初筛分',0),'V1形态通过':setup.get('eligible') is True,
                         'V1形态证据':json.dumps(setup,ensure_ascii=False,allow_nan=False),
                         'V1阻断原因':'；'.join(setup.get('blockers',[])),
                         '触发下沿':levels.get('trigger_price'),'建仓上限':levels.get('entry_price'),
                         '止损参考':levels.get('stop_loss'),'第一止盈':levels.get('take_profit_1'),
                         '第二止盈':levels.get('take_profit_2'),
                         '观察评分':sum(check.get('passed') is True for check in setup.get('checks',[])),
                         '说明':'V1技术形态候选；须prepare及人工审核' if setup.get('eligible') else 'V1形态未通过'})
        except Exception as exc:errors.append(f'{code} {snapshot["name"]}: {type(exc).__name__}: {exc}')
        if mode=='live' and pause>0:time.sleep(pause)
    return pd.DataFrame(rows),errors


def verify_candidates(candidates,mode, max_calls, pause, cfg,benchmark_hist=None):
    if strategy_enabled(cfg):return _verify_v1_candidates(candidates,mode,max_calls,pause,cfg,benchmark_hist)
    rows=[];errors=[]
    begin=(pd.Timestamp.today()-pd.Timedelta(days=125)).strftime('%Y%m%d')
    for i,(_,s) in enumerate(candidates.head(max_calls).iterrows()):
        code=code_str(s['code'])
        try:
            if mode=='demo':
                rng=np.random.default_rng(int(code))
                close=20+pd.Series(rng.normal(.08,.3,55)).cumsum()
                hist=pd.DataFrame({'date':pd.bdate_range(end=pd.Timestamp.today().normalize(),periods=55),'close':close,'high':close+.3,'low':close-.3,'volume':rng.integers(80000,200000,55)})
            else:
                hist=stock_history(code,begin)
                from zoneinfo import ZoneInfo
                now_cn=datetime.now(ZoneInfo('Asia/Shanghai'))
                if now_cn.hour<15 or (now_cn.hour==15 and now_cn.minute<5):
                    today=now_cn.strftime('%Y-%m-%d')
                    if not hist.empty and str(hist.iloc[-1]['date'].date())==today:
                        hist=hist.iloc[:-1].copy()
            item=score_stock(code,str(s['name']),hist)
            chan=analyze_chan(hist)
            item['缠论结构']=chan.get('condition',chan.get('reason','结构不可用'))
            item['缠论调整分']=chan.get('score_adjustment',0) if chan.get('valid') else 0
            item['缠论支撑']=chan.get('support') if chan.get('valid') else None
            item['缠论压力']=chan.get('resistance') if chan.get('valid') else None
            item['初筛分']=s['初筛分']
            item['快照涨跌幅%']=s['pct']
            item['快照成交额(亿)']=round(s['amount']/1e8,2)
            item['快照换手%']=s['turnover']
            item['快照量比']=s['volume_ratio']
            holding=next((x for x in cfg.get('positions',[]) if code_str(x['code'])==code),None)
            levels=make_levels(cfg,code,price=float(hist.iloc[-1]['close']),position=holding,asset_type=holding['type'] if holding else 'stock',price_label='复核日线收盘价')
            item['止损参考']=levels['stop_loss'] if levels else np.nan
            item['第一止盈']=levels['take_profit_1'] if levels else np.nan
            item['第二止盈']=levels['take_profit_2'] if levels else np.nan
            # 只使用历史日线价位；快照价不等于收盘确认。
            rows.append(item)
        except Exception as exc:
            errors.append(f'{code} {s["name"]}: {type(exc).__name__}: {exc}')
        if mode=='live':time.sleep(pause)
    res=pd.DataFrame(rows)
    if not res.empty:res=res.sort_values(['观察评分','初筛分'],ascending=False)
    return res,errors


def rank_sectors(df):
    out=df.copy()
    for key in ('up','down'):
        if key not in out:out[key]=np.nan
    out['上涨占比%']=(out.up/(out.up+out.down).replace(0,np.nan)*100).round(1)
    # 板块当日涨跌 + 涨跌家数宽度；不是资金净流入、不是多日趋势。
    out['板块强度分']=(out['pct'].fillna(0).clip(-10,10)*7 + (out['上涨占比%'].fillna(50)-50)*.45).round(2)
    columns=[x for x in ['name','pct','up','down','上涨占比%','板块强度分','leader','leader_pct'] if x in out]
    return out.sort_values('板块强度分',ascending=False)[columns]


def alert_rules(cfg,prices,universe, verified, mode):
    alerts=[]
    # 用户自定义的关键观察位。仅是“行情信号”，不是自动买卖指令。
    levels=cfg.get('alert_levels',[])
    for item in levels:
        code=code_str(item['code'])
        row=universe[universe.code==code]
        if row.empty:
            continue
        r=row.iloc[0]
        price=float(r.price)
        low=item.get('below');high=item.get('above')
        if low is not None and price<float(low):
            alerts.append({'code':code,'name':item['name'],'type':'跌破关注位','price':price,'level':low,'detail':f'快照价 {price:.3f} 低于 {low:.3f}；仅提示核查'})
        if high is not None and price>float(high):
            alerts.append({'code':code,'name':item['name'],'type':'突破关注位','price':price,'level':high,'detail':f'快照价 {price:.3f} 高于 {high:.3f}；需量价和收盘确认'})
    # 止损止盈单独标识；它们与技术突破/跌破观察位互不混淆。
    # Live mode only: demo reports may show alerts, but will never trigger native notifications.
    watchcodes={code_str(x['code']):x['name'] for x in cfg.get('watchlist',[])}
    positions={code_str(x['code']):x for x in cfg.get('positions',[])
               if not strategy_enabled(cfg) or int(x.get('quantity',0))>0}
    for code,name in {**{x: p['name'] for x,p in positions.items()},**watchcodes}.items():
        pos=positions.get(code)
        px=None
        row=universe[universe.code==code]
        if not row.empty:
            try:px=float(row.iloc[0]['price'])
            except (TypeError,ValueError):pass
        elif pos is not None:
            pair=prices.get((pos['type'],code))
            if pair: px=float(pair[0])
        if px is None or not np.isfinite(px) or px<=0:continue
        plan=make_levels(cfg,code,price=px,position=pos,asset_type=pos['type'] if pos else 'stock',price_label='行情快照价')
        if not plan or not plan['valid_order']:continue
        if px<=plan['stop_loss']:
            alerts.append({'code':code,'name':name,'type':'触及止损参考','price':px,'level':plan['stop_loss'],
                           'detail':f'快照价 {px:.3f} <= 止损参考 {plan["stop_loss"]:.3f}；请核查，非委托单'})
        elif px>=plan['take_profit_2']:
            alerts.append({'code':code,'name':name,'type':'触及第二止盈参考','price':px,'level':plan['take_profit_2'],
                           'detail':f'快照价 {px:.3f} >= 第二止盈 {plan["take_profit_2"]:.3f}；应结合量价决定是否保护利润'})
        elif px>=plan['take_profit_1']:
            alerts.append({'code':code,'name':name,'type':'触及第一止盈参考','price':px,'level':plan['take_profit_1'],
                           'detail':f'快照价 {px:.3f} >= 第一止盈 {plan["take_profit_1"]:.3f}；不代表必须立即卖出'})
    # 全账户预算：持仓价格必须全部得到；ETF和可转债另取快照。
    valuation_cfg={**cfg,'positions':[p for p in cfg.get('positions',[]) if int(p.get('quantity',0))>0]} if strategy_enabled(cfg) else cfg
    if strategy_enabled(cfg) and not valuation_cfg['positions']:
        p_df=pd.DataFrame(columns=['代码','当前快照价','当前市值','浮动盈亏估算','当日参考盈亏估算'])
        missing=[];equity=float(cfg.get('cash',0));loss=float(cfg['baseline_equity'])-equity;remain=None
    else:p_df,missing,equity,loss,remain=portfolio(valuation_cfg,prices,mode)
    if not p_df.empty:
        keys={code_str(x['code']):x for x in cfg.get('positions',[])}
        levels_by_code={}
        for code,p in keys.items():
            pair=prices.get((p['type'],code))
            px=float(pair[0]) if pair else None
            levels_by_code[code]=make_levels(cfg,code,price=px,position=p,asset_type=p['type'])
        for field,attr in [('止损参考价','stop_loss'),('第一止盈价','take_profit_1'),('第二止盈价','take_profit_2')]:
            p_df[field]=p_df['代码'].map(lambda x: (levels_by_code.get(str(x).zfill(6)) or {}).get(attr))
    if missing:
        alerts.append({'code':'PORT','name':'组合风控','type':'数据缺失','price':None,'level':None,'detail':'持仓行情缺失，不能计算预算: '+','.join(missing)})
    elif loss is not None and not strategy_enabled(cfg):
        if loss>=cfg['max_additional_loss']*.8:
            alerts.append({'code':'PORT','name':'组合风控','type':'预算接近/超额','price':equity,'level':cfg['baseline_equity']-cfg['max_additional_loss'],'detail':f'账户快照累计额外亏损¥{loss:.2f}，风险预算¥{cfg["max_additional_loss"]:.2f}，请核对实际持仓及成交价'})
    if strategy_enabled(cfg):
        remain=None
        p_df=p_df.rename(columns={'基准价':'历史基准价（非V1额度）','相对11/6变动':'相对11/6历史变动'})
    return pd.DataFrame(alerts,columns=['code','name','type','price','level','detail']),p_df,missing,equity,loss,remain


def persist_alerts(df,mode,notify=False):
    OUT.mkdir(exist_ok=True)
    path=OUT/'alerts.csv';df.to_csv(path,index=False,encoding='utf-8-sig')
    if not notify or mode=='demo' or df.empty:return 0
    # 无论是否提醒都先去重，同日同代码同类型不重复；macOS通知可被系统过滤。
    keys_path=OUT/'alert_sent.json'
    try: sent=set(json.loads(keys_path.read_text(encoding='utf-8')))
    except (OSError,ValueError):sent=set()
    today=datetime.now().strftime('%Y-%m-%d');cnt=0
    import subprocess
    for _,row in df.iterrows():
        key=f'{today}:{row["code"]}:{row["type"]}'
        if key in sent:continue
        msg=f'{row["name"]}: {row["type"]}，请打开报告核实行情'
        script='display notification '+json.dumps(msg,ensure_ascii=False)+' with title '+json.dumps('A股量化助手',ensure_ascii=False)
        try:
            subprocess.run(['osascript','-e',script],timeout=8,check=True,capture_output=True)
            sent.add(key);cnt+=1
        except Exception as e:print('本地通知失败:',e,file=sys.stderr)
    keys_path.write_text(json.dumps(sorted(x for x in sent if x[:10]>=today),ensure_ascii=False),encoding='utf-8')
    return cnt


def produce_report(mode, universe, pick, verified, sectors, alerts, p_df, missing, equity, loss, errors, sources, max_candidates, daily):
    OUT.mkdir(exist_ok=True)
    for name,table in [('market_candidates.csv',pick),('verified_top.csv',verified),('sector_rank.csv',sectors),('alerts.csv',alerts),('portfolio.csv',p_df)]:
        table.to_csv(OUT/name,index=False,encoding='utf-8-sig')
    def table(df,limit=25):
        if df.empty:return '<p>暂无可展示数据</p>'
        return df.head(limit).to_html(index=False,escape=True,border=0,classes='t',na_rep='—')
    when=datetime.now().astimezone().isoformat(timespec='seconds')
    source_info='；'.join(sources)
    status='演示合成数据（绝不可用于交易）' if mode=='demo' else '网络行情快照（无交易所级时间验证）'
    v1=daily.get('strategy_version')=='1.0'
    risk=('持仓快照不完整，禁止给出当前预算判断' if missing else f'账户快照市值¥{equity:,.2f}；'+
          (f'历史基准差额¥{loss:,.2f}（仅作历史比较，非V1可用风险额度）' if v1 else f'相对基准额外亏损¥{loss:,.2f}（负数代表盈利）'))
    if v1:risk+='；V1调流后高水位与风险预算以盘中核验台账为准，本扫描不发旧基准预算警报'
    if not missing and not p_df.empty:
        floating_col=p_df['浮动盈亏估算']
        daily_col=p_df['当日参考盈亏估算']
        if floating_col.notna().all():
            risk+=f'；浮动盈亏（按成本估算）¥{floating_col.sum():+,.2f}'
        if daily_col.notna().all() and mode=='live':
            risk+=f'；当日参考盈亏（昨收比估算）¥{daily_col.sum():+,.2f}'
        else:
            risk+='；当日参考盈亏不可计算（演示数据或部分证券缺少昨收价）'
    err='<ul>'+''.join('<li>'+html.escape(x)+'</li>' for x in errors[:40])+'</ul>' if errors else '无抓取异常（不代表数据无延迟）'
    drows=pd.DataFrame(daily['shortlist'])
    if not drows.empty:
        fields=(['rank','name','code','asof','close','trigger_price','entry_price','stop_loss','take_profit_1','take_profit_2'] if v1 else ['rank','name','code','asof','close','total_score','chan_condition','stop_loss','take_profit_1','take_profit_2'])
        drows=drows[fields].rename(columns={'rank':'序号','name':'名称','code':'代码','asof':'日线日期','close':'参考收盘价','trigger_price':'固定触发下沿','entry_price':'最高成交价Ecap','total_score':'研究排序分','chan_condition':'简化缠论观察','stop_loss':'止损参考','take_profit_1':'止盈①','take_profit_2':'止盈②'})
    doc=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>A股量化助手 v3</title><style>body{{font-family:-apple-system,system-ui,sans-serif;margin:30px auto;max-width:1250px;padding:0 16px;background:#f6f8fc;color:#253149}}.card{{padding:18px;border-radius:14px;background:white;margin:18px 0;box-shadow:0 2px 14px #e2e5ee}}.scroll{{overflow-x:auto}}.t{{width:100%;border-collapse:collapse;font-size:12px;white-space:nowrap}}td,th{{border-bottom:1px solid #e7e9f0;padding:9px;text-align:left}}th{{background:#eaf0fb}}.warn{{color:#a44e20}}</style><h1>A股 A+B 量化助手 · 网页研究台 v3</h1><div class="card"><strong class="warn">{html.escape(status)}</strong><p>生成：{html.escape(when)}；全市场快照：{len(universe)}只；初筛：{len(pick)}只；日线复核：{len(verified)}只（上限{max_candidates}）</p><p>{html.escape(risk)}</p><small>来源：{html.escape(source_info)}。风险警报不保证止损成功。无券商连接、无自动下单。</small></div><div class="card"><h2>一、组合、止损止盈参考价与提醒</h2>{table(alerts,50)}<h3>持仓详情</h3><div class="scroll">{table(p_df,100)}</div></div><div class="card"><h2>二、每日精选观察（最多5只，不是保证最佳买入）</h2><p>日线日期：{html.escape(str(daily["asof"]))}；不满足筛选条件则不凑数。缠论只做简化结构辅助，未经完整一二三买验证。若为演示数据，严禁据此交易。</p><div class="scroll">{table(drows,5)}</div></div><div class="card"><h2>三、日线复核强势榜</h2><div class="scroll">{table(verified,30)}</div></div><div class="card"><h2>四、全市场快照初筛榜</h2><div class="scroll">{table(pick,30)}</div></div><div class="card"><h2>五、行业强度榜（单日）</h2><div class="scroll">{table(sectors,30)}</div></div><div class="card"><h2>数据错误与限制</h2>{err}<p>强度排名依据当日涨幅和上涨家数占比；不等于资金净流入或多日趋势。股票历史日线可能滞后于盘中快照。历史价格前复权、快照价格未复权，不可跨时间直接比价。浮动盈亏按录入成本、当日盈亏按上一交易日参考价计算，不含交易费用、分红、当日交易与券商成本调整，因此可能与券商数值不一致。ETF/转债行情可能不同步。实际交易后必须更新 config.json 的持仓和现金。周末或节假日执行可能返回旧收盘价。</p></div></html>'''
    if v1:
        doc=doc.replace('缠论只做简化结构辅助，未经完整一二三买验证。','V1只使用固定3日缩量回调后转强形态；候选价不是已保存计划，须服务器prepare和人工审核。')
        doc=doc.replace('三、日线复核强势榜','三、V1完整日线形态复核')
        if daily.get('blockers'):
            doc=doc.replace('<h2>数据错误与限制</h2>','<h2>数据错误与限制</h2><p>'+html.escape('；'.join(daily['blockers']))+'</p>')
    (OUT/'report_v2.html').write_text(doc,encoding='utf-8')


def main():
    ap=argparse.ArgumentParser(description='A股第2阶段，绝不自动交易')
    ap.add_argument('--mode',choices=['demo','live'],default='demo')
    ap.add_argument('--config',default=str(ROOT/'config.json'))
    ap.add_argument('--top',type=int,default=80,help='历史日线复核最多几只，建议30-100')
    ap.add_argument('--notify',action='store_true',help='仅macOS本机通知（不保证送达）')
    args=ap.parse_args();cfg=load_config(args.config)
    if args.top<1 or args.top>300:ap.error('top 要在1—300间')
    errors=[];sources=[]
    try: universe,desc=full_snapshot(args.mode);sources.append(desc)
    except Exception as e:
        if strategy_enabled(cfg):
            daily=select_five(pd.DataFrame(),cfg,args.mode)
            daily.update(status='全市场来源失败，V1无候选；请检查数据源后重新扫描',
                         blockers=['全市场行情不可用：'+type(e).__name__],source_errors=[str(e)])
            OUT.mkdir(exist_ok=True)
            path=OUT/('daily_observe_demo.json' if args.mode=='demo' else 'daily_observe.json')
            temp=path.with_suffix('.tmp');temp.write_text(json.dumps(daily,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8');temp.replace(path)
        print('全市场抓取失败，不输出伪全市场排名:',e,file=sys.stderr);sys.exit(2)
    pick=filter_universe(universe,max(args.top*2,100),cfg)
    verified,fail=verify_candidates(pick,args.mode,args.top,float(cfg.get('history_pause_seconds',0.5)),cfg)
    errors.extend(fail)
    try:sector,desc=sector_snapshot(args.mode);sources.append(desc);sectors=rank_sectors(sector)
    except Exception as e:
        errors.append('行业抓取失败: '+str(e));sectors=pd.DataFrame()
    if args.mode=='live':
        prices,feed_errors=snapshot_akshare();errors.extend(feed_errors)
    else:prices={}
    alerts,p_df,missing,equity,loss,remain=alert_rules(cfg,prices,universe,verified,args.mode)
    daily=select_five(verified,cfg,args.mode)
    if strategy_enabled(cfg):
        daily['source_errors']=errors
        if not daily['count']:
            daily['blockers']=list(dict.fromkeys(daily.get('blockers',[])+errors))[:30]
    dest=OUT/('daily_observe_demo.json' if args.mode=='demo' else 'daily_observe.json')
    OUT.mkdir(exist_ok=True)
    temp=dest.with_suffix('.tmp')
    temp.write_text(json.dumps(daily,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    temp.replace(dest)
    sent=persist_alerts(alerts,args.mode,args.notify)
    produce_report(args.mode,universe,pick,verified,sectors,alerts,p_df,missing,equity,loss,errors,sources,args.top,daily)
    # Dashboard quote cache: snapshot prices have no exchange-confirmed timestamps.
    watch_codes={code_str(x['code']) for x in cfg.get('watchlist',[])}
    watch=universe[universe.code.isin(watch_codes)].copy()
    watch.to_csv(OUT/'watch_quotes.csv',index=False,encoding='utf-8-sig')
    metadata={'mode':args.mode,'generated_at':datetime.now().astimezone().isoformat(timespec='seconds'),
              'scan_ok':True,
              'config_hash':hashlib.sha256(json.dumps(cfg,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
              'universe_count':len(universe),'verified_count':len(verified),
              'source_notes':sources,'errors_count':len(errors),
              'quote_time_verified':False,'daily_observe_count':daily['count'],'daily_observe_asof':daily['asof']}
    tmp=OUT/'run_meta.json.tmp'
    tmp.write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
    tmp.replace(OUT/'run_meta.json')
    print(f'已生成 {OUT/"report_v2.html"}；全市场 {len(universe)}；初筛 {len(pick)}；日线复核 {len(verified)}；提醒 {len(alerts)}；精选观察 {daily["count"]}；本机通知 {sent}')
    if errors:print('抓取异常:',*errors[:10],sep='\n - ')

if __name__=='__main__':main()
