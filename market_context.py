"""Read-only pre-open auction and global indices. Fails closed on stale/ambiguous timestamps.

A-share pre-open minute bars are not the official exchange opening auction feed.
Every display value carries source timestamp and quality label. No pre-open risk trading triggers.
"""
from __future__ import annotations
import datetime as dt
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo

CST=ZoneInfo('Asia/Shanghai')
MARKETS={
 'SP500':{'name':'标普500','region':'US','keywords':['标普500','标普 500','S&P 500','S&P500','标准普尔500'],'codes':['SPX','SP500']},
 'NASDAQ':{'name':'纳斯达克综合','region':'US','keywords':['纳斯达克综合','纳斯达克指数','纳斯达克','NASDAQ COMPOSITE'],'codes':['IXIC','COMP']},
 'DOW':{'name':'道琼斯','region':'US','keywords':['道琼斯工业','道琼斯指数','道琼斯','道指'],'codes':['DJI','DJIA']},
 'NIKKEI':{'name':'日经225','region':'JP','keywords':['日经225','日经 225','日经平均','NIKKEI225'],'codes':['N225','NI225']},
 'TOPIX':{'name':'日本TOPIX','region':'JP','keywords':['东证股价','TOPIX'],'codes':['TOPIX']},
 'KOSPI':{'name':'韩国KOSPI','region':'KR','keywords':['韩国综合','韩国KOSPI','KOSPI','韩国股价综合'],'codes':['KOSPI']},
 'KOSDAQ':{'name':'韩国KOSDAQ','region':'KR','keywords':['韩国创业板','KOSDAQ'],'codes':['KOSDAQ']},
}
TZ={'US':ZoneInfo('America/New_York'),'JP':ZoneInfo('Asia/Tokyo'),'KR':ZoneInfo('Asia/Seoul')}


def num(value):
    try:
        x=float(value)
        return x if math.isfinite(x) else None
    except (ValueError, TypeError):return None


def parse_time(value,region):
    if value is None:return None
    if isinstance(value,dt.datetime):t=value
    else:
        raw=str(value).strip().replace('/','-')
        try:t=dt.datetime.fromisoformat(raw)
        except ValueError:return None
    if t.tzinfo is None:t=t.replace(tzinfo=TZ[region])
    return t.astimezone(dt.timezone.utc)


def match_index(name,code):
    name=str(name).upper().replace(' ','')
    code=str(code).upper().strip()
    # Distinguish broad indexes from NASDAQ100 and KOSPI200 to avoid false comparisons.
    if '纳斯达克100' in name or 'NASDAQ100' in name or code in ('NDX','NAS100'):return None
    if 'KOSPI200' in name or '韩国200' in name or code=='KOSPI200':return None
    # Exact codes are only accepted for indexes with unambiguous symbols.
    for k,meta in MARKETS.items():
        if code in meta['codes']:return k
    for k,meta in MARKETS.items():
        if any(kw.upper().replace(' ','') in name for kw in meta['keywords']):return k
    return None


def parse_global(frame, now):
    """Return named market rows; accept only records with usable source timestamp.
       US: most recent *completed* regular session in NY; JP/KR: current local day.
    """
    now=now.astimezone(dt.timezone.utc)
    result={}
    for _,r in frame.iterrows():
        key=match_index(r.get('名称',''),r.get('代码',''))
        if not key:continue
        meta=MARKETS[key];region=meta['region']
        stamp=parse_time(r.get('最新行情时间'),region)
        price=num(r.get('最新价'));pct=num(r.get('涨跌幅'))
        if not stamp or not price or price<=0 or pct is None or stamp>now+dt.timedelta(minutes=5):continue
        local=stamp.astimezone(TZ[region]);localnow=now.astimezone(TZ[region])
        age=(now-stamp).total_seconds()/60
        if region=='US':
            # A-share early morning occurs well after NY cash close. Ignore still-open US cash sessions.
            completed=(local.hour>=16 or local.date()<localnow.date())
            good=completed and age<=6*24*60 and local.date()<=localnow.date()
            label='美股最近已完成交易日收盘' if good else '美股时点不符合已完成交易日'
        else:
            good=local.date()==localnow.date() and age<=35
            label='日韩当日行情（可能延迟）' if good else '日韩数据过期或休市'
        row={'key':key,'name':meta['name'],'region':region,'price':round(price,3),'pct':round(pct,3),
             'source_time':local.isoformat(timespec='seconds'),'source_timezone':str(TZ[region]),
             'source_age_minutes':round(age,1),'usable':bool(good),'quality':label}
        # Preserve the newest update for duplicate names/codes.
        old=result.get(key)
        if not old or row['source_time']>old['source_time']:result[key]=row
    # Return all slots so users can distinguish unavailable data from zero change.
    rows=[]
    for key,meta in MARKETS.items():
        rows.append(result.get(key,{'key':key,'name':meta['name'],'region':meta['region'],
            'price':None,'pct':None,'source_time':None,'usable':False,'quality':'未找到带有效时间的行情'}))
    return rows


def get_global(now):
    """AKShare's global-index snapshot: one request for US+JP+KR, no invented fallback."""
    try:
        import akshare as ak
        frame=ak.index_global_spot_em()
        if frame is None or frame.empty:raise ValueError('全球指数快照为空')
        for col in ('名称','最新价','涨跌幅','最新行情时间'):
            if col not in frame.columns:raise ValueError('全球指数缺少字段 '+col)
        return {'source':'AKShare / 东方财富全球指数','rows':parse_global(frame,now),
            'fetched_at':now.isoformat(timespec='seconds'),'error':None}
    except Exception as e:
        return {'source':'AKShare / 东方财富全球指数','rows':parse_global(__import__('pandas').DataFrame([]),now),
                'fetched_at':now.isoformat(timespec='seconds'),'error':f'{type(e).__name__}: {e}'}


def current_auction_phase(now):
    t=now.astimezone(CST)
    if t.weekday()>=5:return 'closed'
    minutes=t.hour*60+t.minute
    if 555<=minutes<560:return 'cancellable' # 09:15-09:20
    if 560<=minutes<565:return 'locked'      # 09:20-09:25
    if 565<=minutes<570:return 'confirmation'# 09:25-09:30; no auction orders being accepted
    return 'closed'


def parse_auction(frame,code,name,now,previous_close=None):
    now=now.astimezone(CST)
    if frame is None or frame.empty or '时间' not in frame.columns:
        return {'code':code,'name':name,'price':None,'source_time':None,'usable':False,'quality':'无有效盘前分钟数据'}
    candidates=[]
    for _,r in frame.iterrows():
        try:
            at=dt.datetime.fromisoformat(str(r['时间']).strip().replace('/','-'))
            if at.tzinfo is None:at=at.replace(tzinfo=CST)
            at=at.astimezone(CST)
        except (ValueError,TypeError):continue
        if at.date()!=now.date() or at.time()<dt.time(9,15) or at.time()>dt.time(9,26) or at>now+dt.timedelta(seconds=20):continue
        price=num(r.get('收盘'))
        if not price or price<=0:continue
        candidates.append((at,price,r))
    if not candidates:
        return {'code':code,'name':name,'price':None,'source_time':None,'usable':False,'quality':'数据源尚未返回今天的竞价分钟线'}
    at,price,r=max(candidates,key=lambda x:x[0])
    age=(now-at).total_seconds()/60
    # At 09:25/26 the upstream time label may encode auction result; do not claim official status.
    is_final_hint=at.time()>=dt.time(9,25)
    usable=age<=5
    pct=(price/previous_close-1)*100 if previous_close and previous_close>0 else None
    return {'code':code,'name':name,'price':round(price,3),'source_time':at.isoformat(timespec='seconds'),
        'usable':usable,'quality':('源时间已过期' if not usable else '9:25后参考行情（非交易所认证结果）' if is_final_hint else '竞价参考价格（非实际成交）'),
        'source':'AKShare stock_zh_a_hist_pre_min_em','previous_close':previous_close,
        'indicative_change_pct':round(pct,2) if pct is not None else None,
        'auction_volume_lots':None,'matching_volume_verified':False,
        'status':'结果观察' if is_final_hint else '动态参考'}


def get_auction(cfg,now,limit=12):
    """Only watchlist+positions to avoid hammering upstream per-stock endpoint."""
    allcodes={str(x['code']).zfill(6):x['name'] for x in cfg.get('positions',[]) if x.get('code') and x.get('type')=='stock'}
    allcodes.update({str(x['code']).zfill(6):x['name'] for x in cfg.get('watchlist',[]) if x.get('code')})
    items=list(allcodes.items())[:limit]
    if current_auction_phase(now)=='closed':return {'rows':[],'errors':[],'limited':len(allcodes)>limit,'count':0}
    try:import akshare as ak
    except Exception as e:return {'rows':[],'errors':[str(e)],'limited':False,'count':0}
    rows=[];errors=[]
    def pull(item):
        code,name=item
        f=ak.stock_zh_a_hist_pre_min_em(symbol=code,start_time='09:15:00',end_time='09:27:00')
        return parse_auction(f,code,name,now)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures={pool.submit(pull,item):item for item in items}
        for fut in as_completed(futures):
            try:rows.append(fut.result())
            except Exception as e:
                code,name=futures[fut];errors.append(f'{code} {name}: {type(e).__name__}: {e}')
                rows.append({'code':code,'name':name,'price':None,'source_time':None,'usable':False,'quality':'上游请求失败'})
    rows.sort(key=lambda x:x['code'])
    return {'rows':rows,'errors':errors,'limited':len(allcodes)>limit,'count':len(items)}
