"""V1 journals and verified evidence. Local research only; never executes orders."""
from __future__ import annotations
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import fcntl
import hashlib
import json
import math
import os
import secrets
import threading

CST=ZoneInfo('Asia/Shanghai')
DEFAULTS={'per_trade_risk':150.0,'min_fees':20.0,'max_position_pct':15.0,
          'max_sector_pct':30.0,'max_exposure_pct':60.0,'max_positions':4,
          'loss_budget':1000.0,'pause_drawdown':500.0,'reduce_drawdown':800.0}
_EVIDENCE_LOCK=threading.Lock()
_EVIDENCE_ATTEMPTS={}
_PREPARE_POOL=ThreadPoolExecutor(max_workers=1,thread_name_prefix='v1-prepare')
_PREPARE_LOCK=threading.Lock()
_PREPARE_FLIGHT=None
_PREPARE_KEY=None


def enabled(cfg):return cfg.get('strategy_v1',{}).get('enabled') is True


def read(path,default=None):
    try:return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError,ValueError):return {} if default is None else default


def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.'+secrets.token_hex(4)+'.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    os.replace(tmp,path)


@contextmanager
def locked(output):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    with (output/'strategy.lock').open('a') as f:
        fcntl.flock(f,fcntl.LOCK_EX)
        try:yield
        finally:fcntl.flock(f,fcntl.LOCK_UN)


def number(value,low=0,high=1e12):
    try:x=float(value)
    except (ValueError,TypeError):raise ValueError('需要有效数值')
    if not math.isfinite(x) or not low<=x<=high:raise ValueError('数值超出允许范围')
    return x


def iso_day(value):
    try:d=date.fromisoformat(str(value))
    except (TypeError,ValueError):raise ValueError('日期应为 YYYY-MM-DD')
    if d>datetime.now(CST).date():raise ValueError('日期不能晚于今天')
    return str(d)


def ledger_hash(cfg):
    rows=[{k:p.get(k) for k in ('code','type','quantity','cost')} for p in cfg.get('positions',[]) if int(p.get('quantity',0))>0]
    return hashlib.sha256(json.dumps({'cash':cfg.get('cash'),'positions':sorted(rows,key=lambda p:p['code'])},sort_keys=True).encode()).hexdigest()


def settings(cfg):
    result={**DEFAULTS,**{k:v for k,v in cfg.get('strategy_v1',{}).items() if k in DEFAULTS}}
    result.update(fee_reserve=result['min_fees'],account_risk_budget=result['loss_budget'])
    return result


def validate_settings(body):
    # Version 1 risk limits can be tightened, not silently relaxed beyond the design.
    ranges={'per_trade_risk':(1,150),'min_fees':(20,10000),'max_position_pct':(1,15),
            'max_sector_pct':(1,30),'max_exposure_pct':(1,60),'max_positions':(1,4),
            'loss_budget':(1,1000),'pause_drawdown':(1,500),'reduce_drawdown':(1,800)}
    out={k:number(body.get(k,DEFAULTS[k]),*ranges[k]) for k in DEFAULTS}
    if out['max_positions']!=int(out['max_positions']):raise ValueError('最大持仓数必须为整数')
    out['max_positions']=int(out['max_positions'])
    if not out['pause_drawdown']<=out['reduce_drawdown']<=out['loss_budget']:raise ValueError('暂停、减风险阈值不能超过总预算')
    if out['min_fees']>=out['per_trade_risk']:raise ValueError('费用预留需低于单笔风险')
    return out


def calendar_dates(output, now=None):
    cache=read(Path(output)/'strategy_calendar.json')
    ds=cache.get('dates',[])
    if not cache.get('verified') or not isinstance(ds,list):return []
    try:
        ds=sorted(set(str(date.fromisoformat(x)) for x in ds))
        if not ds or ds[-1]<str((now or datetime.now(CST)).date()):return []
    except (TypeError,ValueError):return []
    return ds


def latest_completed_day(output, now=None):
    clock = (now or datetime.now(CST)).astimezone(CST)
    today = clock.date().isoformat()
    include_today = clock.hour * 60 + clock.minute >= 905
    return next((day for day in reversed(calendar_dates(output, clock))
                 if day < today or (day == today and include_today)), None)


def _account_view(output,cfg,account,now):
    account=dict(account)
    account['needs_reconcile']=bool(account.get('needs_reconcile') or (account.get('initialized') and account.get('ledger_hash')!=ledger_hash(cfg)))
    # Displaying the journal never certifies that prices remain fresh.
    account['verified']=bool(account.get('verified') and not account['needs_reconcile'])
    account['opens_today']=len({x.get('code') for x in _lines(Path(output)/'manual_trade_log.jsonl')
                                if x.get('side')=='buy' and x.get('date')==str(now.date())} |
                               {str(p['code']) for p in cfg.get('positions',[]) if int(p.get('quantity',0))>0
                                and cfg.get('trade_plans',{}).get(str(p['code']),{}).get('buy_date')==str(now.date())
                                and cfg.get('trade_plans',{}).get(str(p['code']),{}).get('actual_entry')})
    reviews=read(Path(output)/'strategy_reviews.json',[])
    losses=0
    for review in reversed(reviews):
        if review.get('net_pnl',0)<0:losses+=1
        else:break
    account['consecutive_losses']=losses
    pauses=[x for x in reviews if x.get('pause_trigger')]
    if pauses:
        last=pauses[-1]; ds=calendar_dates(output)
        following=[d for d in ds if d>last['recorded_on']]
        account['pause_until']=following[1] if len(following)>=2 else None
        account['pause_calendar_pending']=len(following)<2
        account['paused']=bool(len(following)<2 or str(now.date())<=following[1])
        if not account['paused']:
            account['last_pause_until']=account['pause_until'];account['pause_until']=None
    else:account.update(paused=False,pause_until=None)
    if account.get('budget_exhausted'):
        account['paused']=True
        account['pause_reason']='V1 回撤预算已耗尽，需复盘并人工确认恢复；高水位不重置'
    return account


def _lines(path):
    try:
        return [json.loads(x) for x in path.read_text(encoding='utf-8').splitlines() if x.strip()]
    except (OSError,ValueError):return []


def get_account(output,cfg,now=None):
    now=now or datetime.now(CST)
    raw=read(Path(output)/'strategy_account.json',{'initialized':False,'verified':False})
    if not isinstance(raw,dict):raw={'initialized':False,'verified':False,'needs_reconcile':True,'error':'V1账户记录损坏，需恢复备份'}
    return _account_view(output,cfg,raw,now)


def observe(output,cfg,equity,*,verified=False,at=None,reconcile=False):
    """Only a complete source-verified poll may initialize/advance the V1 high-water mark."""
    now=at or datetime.now(CST)
    if not enabled(cfg):return {}
    if not verified:return get_account(output,cfg,now)
    equity=number(equity,0.01)
    with locked(output):
        path=Path(output)/'strategy_account.json'; a=read(path)
        if not isinstance(a,dict):raise ValueError('V1账户记录损坏，禁止重置高水位')
        if a.get('initialized'):
            for key in ('initial_equity','high_water','adjusted_equity','drawdown','net_external_flow'):
                number(a.get(key),-1e12 if key in ('adjusted_equity','net_external_flow') else 0)
        fingerprint=ledger_hash(cfg)
        if a.get('initialized') and a.get('ledger_hash')!=fingerprint:a['needs_reconcile']=True
        if a.get('needs_reconcile') and not reconcile:
            a['verified']=False;write(path,a)
            return _account_view(output,cfg,a,now)
        flows=read(Path(output)/'strategy_cashflows.json',[])
        if not a.get('initialized'):
            a={'initialized':True,'initial_equity':equity,'initialized_at':now.isoformat(),'high_water':equity,
               'flow_ids_at_start':[x['id'] for x in flows],'net_external_flow':0.0}
        flow_ids=set(a.get('flow_ids_at_start',[]))
        flow=sum(float(x['amount']) for x in flows if x['id'] not in flow_ids)
        adjusted=equity-flow; high=max(float(a['high_water']),adjusted)
        dd=max(0,high-adjusted)
        a.update(verified=True,equity=round(equity,2),adjusted_equity=round(adjusted,2),
                 high_water=round(high,2),drawdown=round(dd,2),net_external_flow=round(flow,2),
                 ledger_hash=fingerprint,needs_reconcile=False,observed_at=now.isoformat())
        if dd>=settings(cfg)['loss_budget']:
            if not a.get('budget_exhausted'):a['budget_exhausted_at']=now.isoformat()
            a['budget_exhausted']=True
        write(path,a)
    return _account_view(output,cfg,a,now)


def add_flow(output,cfg,body):
    amount=number(body.get('amount'),-1e9,1e9)
    if not amount:raise ValueError('净转账金额不能为零')
    day=iso_day(body.get('date')); a=get_account(output,cfg)
    if not a.get('initialized'):raise ValueError('先用完整真实行情初始化 V1，历史转账不计入新体系')
    if day<a['initialized_at'][:10]:raise ValueError('只能记录 V1 初始化后的净外部转账')
    entry={'id':secrets.token_hex(8),'date':day,'amount':amount,'note':str(body.get('note',''))[:300],
           'recorded_at':datetime.now(CST).isoformat()}
    with locked(output):
        path=Path(output)/'strategy_cashflows.json';flows=read(path,[]);flows.append(entry);write(path,flows)
        a=read(Path(output)/'strategy_account.json');a.update(needs_reconcile=True,verified=False);write(Path(output)/'strategy_account.json',a)
    return entry


def add_review(output,cfg,body):
    code=str(body.get('code','')).strip()
    if len(code)!=6 or not code.isdigit():raise ValueError('复盘需要6位证券代码')
    opened=iso_day(body.get('opened_on'));closed=iso_day(body.get('closed_on'))
    if closed<opened:raise ValueError('平仓日期不能早于买入日期')
    pnl=number(body.get('net_pnl'),-1e9,1e9);risk=number(body.get('planned_risk'),0.01,1e9)
    review={'id':secrets.token_hex(8),'code':code,'opened_on':opened,'closed_on':closed,
            'net_pnl':pnl,'planned_risk':risk,'net_r':round(pnl/risk,4),'violation':body.get('violation') is True,
            'note':str(body.get('note',''))[:1000],'recorded_on':str(datetime.now(CST).date()),'recorded_at':datetime.now(CST).isoformat()}
    with locked(output):
        path=Path(output)/'strategy_reviews.json';rows=read(path,[])
        if any(x['code']==code and x['opened_on']==opened and x['closed_on']==closed for x in rows):raise ValueError('这笔完整交易已复盘，不能重复计数')
        losses=0
        for r in reversed(rows+[review]):
            if r['net_pnl']<0:losses+=1
            else:break
        review['pause_trigger']=review['violation'] or (pnl<0 and losses>=3)
        rows.append(review);write(path,rows)
    return review


def resume(output,cfg,body):
    if body.get('acknowledged') is not True:raise ValueError('需确认已完成复盘，恢复不会重置回撤')
    with locked(output):
        path=Path(output)/'strategy_account.json';a=read(path)
        if not a.get('budget_exhausted'):raise ValueError('当前没有预算耗尽锁定')
        reviews=read(Path(output)/'strategy_reviews.json',[])
        if not any(x.get('recorded_at','')>a.get('budget_exhausted_at','') for x in reviews):raise ValueError('先记录本次触发后的完整交易复盘')
        a['budget_exhausted']=False;a['resumed_at']=datetime.now(CST).isoformat();write(path,a)
    return get_account(output,cfg)


def status(output,cfg):
    account=get_account(output,cfg); plans=cfg.get('trade_plans',{})
    legacy=[]
    for p in cfg.get('positions',[]):
        if int(p.get('quantity',0))<=0:continue
        plan=plans.get(str(p['code']),{})
        missing=[]
        if not plan.get('stop_loss'):missing.append('固定退出价')
        if not (p.get('sector') or plan.get('sector')):missing.append('行业分类')
        if not plan.get('buy_date') and cfg.get('strategy_v1',{}).get('exit_mode')!='price_first':missing.append('真实买入日期')
        legacy.append({**p,'missing':missing,'legacy':str(p['code']) in cfg.get('strategy_v1',{}).get('legacy_codes',[])})
    blockers=[]
    if not account.get('initialized'):blockers.append('等待完整、源时间已核验的实时持仓行情初始化 V1')
    if account.get('needs_reconcile'):blockers.append('持仓或现金、外部转账发生变动，需核对台账')
    if account.get('paused'):blockers.append(account.get('pause_reason') or '连续亏损/违规冷静期')
    blockers.extend(f"{p['name']}待补：{'、'.join(p['missing'])}" for p in legacy if p['missing'])
    return {'enabled':enabled(cfg),'settings':settings(cfg),'account':account,'blockers':blockers,
            'legacy_positions':legacy,'plans':plans,'flows':read(Path(output)/'strategy_cashflows.json',[]),
            'reviews':read(Path(output)/'strategy_reviews.json',[]),'calendar_verified':bool(calendar_dates(output))}


def _frame_records(frame):
    import pandas as pd
    frame=frame.rename(columns={'日期':'date','开盘':'open','收盘':'close','最高':'high','最低':'low','成交量':'volume','成交额':'amount'}).copy()
    frame['date']=pd.to_datetime(frame['date'],errors='coerce')
    frame=frame.dropna(subset=['date']).sort_values('date').drop_duplicates('date')
    now=datetime.now(CST)
    # A daily bar is completed only after the market close dissemination buffer.
    cutoff=now.date() if now.hour*60+now.minute>=905 else now.date()-timedelta(days=1)
    frame=frame[frame.date.dt.date<=cutoff]
    frame['date']=frame.date.dt.strftime('%Y-%m-%d')
    cols=[x for x in ('date','open','high','low','close','volume','amount') if x in frame]
    return json.loads(frame[cols].to_json(orient='records'))


def evidence(output,code):
    return read(Path(output)/'strategy_evidence'/f'{code}.json')


def history_frame(item):
    import pandas as pd
    rows=item.get('history',[])
    if not rows:return None
    try:
        df=pd.DataFrame(rows);df['date']=pd.to_datetime(df['date']);return df
    except (KeyError,TypeError,ValueError):return None


def refresh_evidence(output,cfg,code=None):
    """Slow daily/history data is fetched off the 10-second quote polling path."""
    import akshare as ak
    from asset_history import history_by_asset, benchmark_history
    output=Path(output);errors=[];clock=datetime.now(CST);today=str(clock.date())
    cached_calendar=read(output/'strategy_calendar.json')
    if not (calendar_dates(output) and cached_calendar.get('fetched_at','')[:10]==today):
        try:
            dates=ak.tool_trade_date_hist_sina()['trade_date']
            ds=sorted(set(str(x)[:10] for x in dates))
            if today<=ds[-1]:write(output/'strategy_calendar.json',{'verified':True,'dates':ds,'fetched_at':clock.isoformat()})
        except Exception as exc:errors.append('交易日历：'+type(exc).__name__)
    expected=latest_completed_day(output, clock)
    refresh_errors={}
    def store_evidence(c,frame):
        rows=_frame_records(frame)
        actual=rows[-1].get('date') if rows else None
        if not expected or actual!=expected:
            raise ValueError(f'应更新至{expected or "待核验交易日"}，上游日线仅到{actual or "无数据"}')
        write(output/'strategy_evidence'/f'{c}.json',{'history':rows,'fetched_at':datetime.now(CST).isoformat()})
    def failed(c,label,exc):
        detail=type(exc).__name__+('：'+str(exc)[:160] if isinstance(exc,(ValueError,RuntimeError)) else '')
        refresh_errors[c]=detail;errors.append(label+'：'+detail)
    def ready(c):
        cache=evidence(output,c);rows=cache.get('history') or []
        return bool(expected and len(rows)>=120 and cache.get('fetched_at','')[:10]==today and rows[-1].get('date')==expected)
    begin=(clock-timedelta(days=3*365+60)).strftime('%Y%m%d')
    if not ready('benchmark'):
        try:
            store_evidence('benchmark',benchmark_history(begin,clock.strftime('%Y%m%d')))
        except Exception as exc:failed('benchmark','沪深300日线',exc)
    items={str(p['code']):p for p in cfg.get('positions',[]) if int(p.get('quantity',0))>0 and (cfg.get('strategy_v1',{}).get('exit_mode')=='price_first' or cfg.get('trade_plans',{}).get(str(p['code']),{}).get('buy_date'))}
    if code:
        items[code]=next((p for p in cfg.get('positions',[])+cfg.get('watchlist',[]) if str(p['code'])==code),{'code':code,'type':'stock'})
    for c,p in items.items():
        if ready(c):continue
        try:store_evidence(c,history_by_asset(c,p.get('type','stock'),begin))
        except Exception as exc:failed(c,c+'日线',exc)
    cached_asof={}
    for c in ['benchmark',*items]:
        rows=evidence(output,c).get('history') or []
        cached_asof[c]={'asof':rows[-1].get('date') if rows else None,'expected_asof':expected,
                        'refresh_error':refresh_errors.get(c)}
    write(output/'strategy_evidence'/'status.json',{'at':datetime.now(CST).isoformat(),'expected_asof':expected,
           'errors':errors,'by_code':cached_asof})
    return errors


def refresh_evidence_async(output,cfg):
    now=datetime.now(CST)
    # Crossing the daily-completion cutoff must bypass a recent intraday attempt;
    # otherwise a 14:59 fetch postpones the first complete closing bar to 15:14.
    phase='completed' if now.hour*60+now.minute>=905 else 'intraday'
    key=(str(Path(output).resolve()),str(now.date()),phase)
    # Retry at most every 15 min; don't slow or multiply quote requests.
    prior=_EVIDENCE_ATTEMPTS.get(key)
    if prior and (now-prior).total_seconds()<900:return
    if not _EVIDENCE_LOCK.acquire(False):return
    _EVIDENCE_ATTEMPTS[key]=now
    def run():
        try:refresh_evidence(output,cfg)
        finally:_EVIDENCE_LOCK.release()
    threading.Thread(target=run,daemon=True).start()


def prepare(output,cfg,code):
    from strategy_setup import build_setup
    import pandas as pd
    errors=refresh_evidence(output,cfg,code)
    item=next((p for p in cfg.get('positions',[])+cfg.get('watchlist',[]) if str(p['code'])==code),None)
    if item is None:raise ValueError('先添加为观察证券')
    hist=history_frame(evidence(output,code));bench=history_frame(evidence(output,'benchmark'))
    result=build_setup(hist if hist is not None else pd.DataFrame(),bench if bench is not None else pd.DataFrame(),asset_type=item.get('type','stock'),name=item.get('name',''))
    ds=calendar_dates(output)
    if not ds:result.setdefault('blockers',[]).append('交易日历未核验')
    if result.get('signal_date'):
        following=[d for d in ds if d>result['signal_date']]
        if not following:result.setdefault('blockers',[]).append('下一交易日尚未核验')
        else:result['valid_on']=following[0]
    valid=bool(result.get('valid') and not result.get('blockers'))
    pid=secrets.token_hex(16) if valid else None
    if pid:
        with locked(output):
            path=Path(output)/'strategy_prepared.json';rows=read(path);rows[pid]={'code':code,'setup':result,'created_at':datetime.now(CST).isoformat()};write(path,rows)
    return {'ok':valid,'setup':result,'prepared_id':pid,'blockers':result.get('blockers',[]),'data_errors':errors}


def prepare_bounded(output,cfg,code,timeout=15):
    """One slow daily-data job; an unavailable upstream must not occupy web workers."""
    global _PREPARE_FLIGHT,_PREPARE_KEY
    key=(str(Path(output).resolve()),code,hashlib.sha256(json.dumps(cfg,sort_keys=True).encode()).hexdigest())
    with _PREPARE_LOCK:
        if _PREPARE_FLIGHT is not None and _PREPARE_KEY!=key and not _PREPARE_FLIGHT.done():
            return {'ok':False,'blockers':['另一只证券的日线仍在核验，请稍后再试']}
        if _PREPARE_FLIGHT is None or _PREPARE_KEY!=key:
            _PREPARE_KEY=key;_PREPARE_FLIGHT=_PREPARE_POOL.submit(prepare,output,cfg,code)
        flight=_PREPARE_FLIGHT
    try:result=flight.result(timeout=timeout)
    except FutureTimeout:return {'ok':False,'blockers':['日线接口未及时返回，未生成可交易计划；稍后重试获取核验结果']}
    except Exception as exc:result={'ok':False,'blockers':['真实日线核验失败：'+type(exc).__name__]}
    with _PREPARE_LOCK:
        if _PREPARE_FLIGHT is flight:_PREPARE_FLIGHT=None;_PREPARE_KEY=None
    return result


def validate_metadata(output,cfg,code,body,fixed):
    """Server evidence, never a client setup_verified checkbox, grants entry eligibility."""
    holding=next((p for p in cfg.get('positions',[]) if str(p['code'])==code and int(p.get('quantity',0))>0),None)
    prior=cfg.get('trade_plans',{}).get(code,{})
    plan={**(prior if holding else {}),**fixed,'strategy_version':'1.0'}
    plan['legacy_exit_only']=bool(holding and body.get('legacy_exit_only',prior.get('legacy_exit_only',True)))
    sector=str(body.get('sector',prior.get('sector',(holding or {}).get('sector','')))).strip()
    # Exit protection for an existing holding must not wait for industry entry
    # qualifications. Unknown sectors remain unknown and still block NEW risk.
    if len(sector)>40 or (not sector and not plan['legacy_exit_only']):raise ValueError('请填写行业/主题分类（同类ETF与股票使用同一分类）')
    plan['sector']=sector
    locked_plan=bool(holding or prior.get('valid_on','')>=str(datetime.now(CST).date()))
    if locked_plan and prior.get('stop_loss') and plan['stop_loss']<float(prior['stop_loss']):raise ValueError('固定止损只允许上移')
    plan['initial_stop_loss']=prior.get('initial_stop_loss',prior.get('stop_loss',plan['stop_loss'])) if locked_plan else plan['stop_loss']
    if prior and locked_plan:
        for key in ('entry_price','take_profit_1','take_profit_2'):
            if prior.get(key) and abs(float(prior[key])-plan[key])>1e-8:
                raise ValueError('已锁定的入场上限与目标价不可重写，请保留原计划复盘')
    if not plan['legacy_exit_only']:
        pid=str(body.get('prepared_id',prior.get('prepared_id','') if holding else '')); proof=read(Path(output)/'strategy_prepared.json').get(pid)
        if not proof or proof.get('code')!=code:raise ValueError('先获取服务器核验的 V1 形态计划')
        setup=proof['setup']
        executed=bool(holding and prior.get('prepared_id')==pid and prior.get('setup_verified') is True)
        if not executed and setup.get('valid_on','')<str(datetime.now(CST).date()):raise ValueError('次日计划已过期，请重新生成')
        for k,v in setup['levels'].items():
            if k=='stop_loss' and executed:
                # A filled plan retains its original targets/S0, while its
                # protective stop can move up during position management.
                continue
            if k in fixed and abs(float(fixed[k])-float(v))>1e-8:raise ValueError('固定价位必须与已核验形态一致')
            plan[k]=v
        plan['initial_stop_loss']=setup['levels']['stop_loss']
        plan.update(prepared_id=pid,setup_verified=True,signal_date=setup['signal_date'],valid_on=setup['valid_on'],setup_evidence=setup.get('evidence',{}))
        review=body.get('review',prior.get('review',{}) if executed else {})
        if not isinstance(review,dict):raise ValueError('人工核验字段无效')
        plan['review']={k:review.get(k) is True for k in ('fundamentals_ok','event_clear','corporate_action_clear')}
        if not all(plan['review'].values()):raise ValueError('财务/ETF资格、公告及除权变化须全部人工核验')
        plan['review']['resistance_clear']=review.get('resistance_clear') is True
        if not plan['review']['resistance_clear']:raise ValueError('须核验附近压力位，可见上行空间至少2R')
        if body.get('resistance_price') not in ('',None):
            plan['resistance_price']=number(body['resistance_price'],0.001)
            if plan['resistance_price']<plan['take_profit_1']:raise ValueError('附近压力位不足2R，跳过该计划')
    else:plan['setup_verified']=False
    for k in ('buy_date',):
        if body.get(k):
            value=iso_day(body[k])
            if holding and prior.get(k) and prior[k]!=value:raise ValueError('已锁定真实买入日期不能重写')
            plan[k]=value
    for k in ('actual_entry','initial_quantity','tp1_sold_quantity'):
        if body.get(k) not in ('',None):
            value=number(body[k],0 if k=='tp1_sold_quantity' else 0.001)
            if k!='actual_entry':
                if value!=int(value):raise ValueError('成交股数必须为整数')
                value=int(value)
            if holding and k in ('actual_entry','initial_quantity') and prior.get(k) and prior[k]!=value:raise ValueError('已锁定的原始成交价与数量不能重写')
            if k=='tp1_sold_quantity' and value<float(prior.get(k,0)):raise ValueError('已登记第一止盈成交数量不能减少')
            plan[k]=value
    if holding:
        initial=plan.get('initial_quantity',int(holding['quantity']))
        sold=plan.get('tp1_sold_quantity',0)
        if sold>initial or initial-sold<int(holding['quantity']):raise ValueError('初始数量、已卖数量与剩余持仓不一致')
        if sold and (not plan.get('actual_entry') or plan['stop_loss']<plan['actual_entry']):raise ValueError('第一止盈已成交后，剩余仓止损应至少上移至原成交价')
    return plan
