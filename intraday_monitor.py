"""V3.1 A股盘中轮询监控：仅研究提醒，不下单，不保证交易所级实时。"""
from __future__ import annotations
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import threading
from zoneinfo import ZoneInfo

from app import ROOT, OUT, code_str, load_config
from portfolio_quotes import snapshot_selected_akshare, QUOTE_SOURCE
from risk_engine import compute
from trade_levels import make_levels
from market_context import current_auction_phase, get_auction, get_global
from trade_signals import build_signals
from quote_health import assess
from risk_extensions import tier_alerts,risk_level
from portfolio_journal import record_equity
from account_snapshot import save_display_snapshot
from alert_journal import capture_rows, persist_cycle
import strategy_runtime as strategy
import t_runtime as t_runtime

CST = ZoneInfo('Asia/Shanghai')
STATUS_FILE=OUT/'intraday_status.json'
SENT_FILE=OUT/'intraday_sent.json'
HISTORY_FILE=OUT/'intraday_events.jsonl'
CAL_FILE=OUT/'trade_calendar_cache.json'
LOCK_FILE=OUT/'intraday_monitor.lock'
CONTEXT_FILE=OUT/'market_context.json'
SIGNAL_SENT=OUT/'trade_signal_sent.json'
SIGNAL_HISTORY=OUT/'trade_signal_events.jsonl'
EQUITY_JOURNAL=OUT/'equity_observations.jsonl'
_CONTEXT_LOCK=threading.Lock()
_CONTEXT_LAST=0.0


def now_cn():
    return dt.datetime.now(CST)


def in_session(t:dt.datetime):
    if t.weekday()>=5: return False
    hm=t.hour*60+t.minute
    return (570<=hm<690) or (780<=hm<900) # 09:30—11:30；13:00—15:00北京时间


def valid_price(value):
    try:
        value=float(value)
        return value if math.isfinite(value) and value>0 else None
    except (ValueError,TypeError):return None


def store_json(path:Path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    os.replace(tmp,path)


def read_json(path:Path, default):
    try:return json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError):return default


def get_global_throttled(now, seconds=300):
    """Avoid slowing the 10s *portfolio* polls by refetching global indices
    on every cycle. Global data stays separately timestamped in its own payload.
    """
    prior=read_json(CONTEXT_FILE,{})
    global_data=prior.get('global') if prior.get('mode')=='live' else None
    if isinstance(global_data,dict) and global_data.get('fetched_at'):
        try:
            at=dt.datetime.fromisoformat(global_data['fetched_at'])
            if at.tzinfo is None:raise ValueError('missing timezone')
            age=(now-at.astimezone(CST)).total_seconds()
            if 0<=age<seconds:
                return global_data
        except (ValueError,TypeError):pass
    return get_global(now)




def refresh_context_async(cfg, now, demo=False):
    """Decoupled slow overseas/auction calls from priority portfolio quote path."""
    global _CONTEXT_LAST
    interval=90 if current_auction_phase(now)!='closed' else 300
    moment=time.monotonic()
    if moment-_CONTEXT_LAST<interval:return
    if not _CONTEXT_LOCK.acquire(blocking=False):return
    _CONTEXT_LAST=moment
    def worker():
        try:
            phase=current_auction_phase(now)
            old=read_json(CONTEXT_FILE,{})
            if phase!='closed':auction=get_auction(cfg,now)
            else:
                prev=old.get('auction',{})
                auction={'rows':[{**x,'usable':False,'quality':'此前竞价历史参考'} for x in prev.get('rows',[])],
                         'errors':[],'limited':prev.get('limited',False)}
            global_row=get_global_throttled(now)
            data={'mode':'live','phase':phase,'status':'独立行情背景采集（非持仓报价）',
                  'auction':auction,'global':global_row,'generated_at':now.isoformat(),
                  'warning':'海外行情与持仓不同步，不能生成交易信号'}
            store_json(CONTEXT_FILE,data)
        except Exception as exc:
            # Background context errors must not overwrite valid portfolio risk.
            print('海外/竞价背景数据更新失败:',repr(exc),file=sys.stderr,flush=True)
        finally:_CONTEXT_LOCK.release()
    threading.Thread(target=worker,daemon=True,name='market-context').start()

def verified_trading_date(today:dt.date, demo:bool):
    if demo:return True,'模拟模式：不验证交易日历'
    if today.weekday()>=5:return False,'周末休市'
    cached=read_json(CAL_FILE,{})
    if cached.get('checked')==str(today):
        if cached.get('is_open') is not None:
            return cached.get('is_open') is True,cached.get('source','交易日历缓存')
        try:
            retry=dt.datetime.fromisoformat(cached.get('retry_after',''))
            if dt.datetime.now(dt.timezone.utc)<retry:
                return False,cached.get('source','交易日历暂不可用，暂停提醒')
        except (TypeError,ValueError):pass
    try:
        import akshare as ak
        frame=ak.tool_trade_date_hist_sina()
        if 'trade_date' not in frame.columns or frame.empty:
            raise ValueError('trade_date 字段缺失')
        dates=set(str(x)[:10] for x in frame['trade_date'].astype(str))
        # If the source has not updated to the requested year, don't assume a weekday is a trading day.
        if not any(x.startswith(str(today.year)) for x in dates):
            raise ValueError('交易日历不含本年度数据')
        is_open=str(today) in dates
        reason='AKShare 新浪交易日历已验证' if is_open else '经交易日历核对：休市'
        store_json(CAL_FILE,{'checked':str(today),'is_open':is_open,'source':reason})
        return is_open,reason
    except Exception as e:
        reason=f'交易日历核验失败，已禁止提醒：{type(e).__name__}: {e}'
        store_json(CAL_FILE,{'checked':str(today),'is_open':None,'source':reason,
            'retry_after':(dt.datetime.now(dt.timezone.utc)+dt.timedelta(minutes=15)).isoformat()})
        return False,reason


def example_quotes(cfg):
    """示例行情只供验收 UI/逻辑；不产生本机通知。"""
    quotes={}
    for p in cfg.get('positions',[]):
        quotes[(p['type'],code_str(p['code']))]=(float(p['baseline_price']),p['name'])
    for p in cfg.get('watchlist',[]):
        code=code_str(p['code'])
        # Non-holding examples are deliberately labeled demo, never passed off as real quotes.
        if ('stock',code) not in quotes:
            quotes[('stock',code)]=(float({'600406':23.86,'002714':44.40,'000682':10.25}.get(code,10.0)),p['name'])
    return quotes,[]


def evaluate(cfg, feed, *, demo=False,strategy_account=None):
    positions={code_str(p['code']):p for p in cfg.get('positions',[]) if int(p.get('quantity',0))>0}
    all_items={code_str(x['code']):x['name'] for x in cfg.get('watchlist',[])}
    watch_types={code_str(x['code']):x.get('type','stock') for x in cfg.get('watchlist',[])}
    all_items.update({code:p['name'] for code,p in positions.items()})
    quotes={}
    previous_closes={}
    for code,name in all_items.items():
        p=positions.get(code)
        kind=p['type'] if p else watch_types.get(code,'stock')
        key=(kind,code)
        q=feed.get(key)
        price=valid_price(q[0]) if q else None
        prev=valid_price(q[2]) if q and len(q)>2 and not demo else None
        if prev is not None and p:
            previous_closes[code]=prev
        quotes[code]={'code':code,'name':name,'price':price,'held':bool(p),'asset_type':kind,
                      'source_timestamp':q[3] if q and len(q)>3 and not demo else None}
        if q and len(q)>4 and isinstance(q[4],dict):quotes[code].update(q[4])
        if price is not None:
            levels=make_levels(cfg,code,price=price,position=p,asset_type=kind)
            # Watch-only dynamic, percentage-derived levels change with price; only display them.
            quotes[code]['stop_loss']=levels['stop_loss'] if levels else None
            quotes[code]['take_profit_1']=levels['take_profit_1'] if levels else None
            quotes[code]['take_profit_2']=levels['take_profit_2'] if levels else None
    events=[]
    for c in cfg.get('alert_levels',[]):
        code=code_str(c['code']);r=quotes.get(code)
        if not r or r['price'] is None:continue
        price=r['price'];lo=valid_price(c.get('below'));hi=valid_price(c.get('above'))
        if lo is not None and price<=lo:
            events.append({'code':code,'name':r['name'],'kind':'跌破观察价','level':lo,'price':price})
        if hi is not None and price>=hi:
            events.append({'code':code,'name':r['name'],'kind':'突破观察价','level':hi,'price':price})
    for code,r in quotes.items():
        if r['price'] is None:continue
        # Only positions or manually locked trade plans can trigger stop/take-profit alarms.
        if code not in positions and code not in cfg.get('trade_plans',{}):continue
        lv=make_levels(cfg,code,price=r['price'],position=positions.get(code),asset_type=r['asset_type'])
        if not lv or not lv.get('valid_order'):continue
        price=r['price']
        if price<=lv['stop_loss']:
            events.append({'code':code,'name':r['name'],'kind':'触及止损参考','level':lv['stop_loss'],'price':price})
        elif price>=lv['take_profit_2']:
            events.append({'code':code,'name':r['name'],'kind':'触及第二止盈','level':lv['take_profit_2'],'price':price})
        elif price>=lv['take_profit_1']:
            events.append({'code':code,'name':r['name'],'kind':'触及第一止盈','level':lv['take_profit_1'],'price':price})
    held_prices={code:r['price'] for code,r in quotes.items() if code in positions and r['price'] is not None}
    risk=compute(cfg,held_prices,fresh=all(code in held_prices for code in positions),strategy_account=strategy_account,
                 previous_closes=previous_closes,watch_quotes=[{'code':r['code'],'name':r['name'],'price':r['price'],'asset_type':r['asset_type']}
                               for r in quotes.values() if r['asset_type'] in ('stock','etf')])
    if demo:
        from pnl_engine import calculate as calculate_pnl
        risk['pnl']=calculate_pnl(cfg,{},valid=False)
    if risk['valid']:
        events.extend(tier_alerts(risk))
    return quotes,events,risk


def dedup_events(events,day, *, demo):
    saved=read_json(SENT_FILE,{'date':day,'sent':[]})
    used=set(saved.get('sent',[])) if saved.get('date')==day else set()
    new=[]
    for e in events:
        identity=e.get("tier",e["level"])
        key=f"{e['code']}|{e['kind']}|{identity}"
        if key not in used:
            new.append(e);used.add(key)
    if not demo:
        store_json(SENT_FILE,{'date':day,'sent':sorted(used)})
    return new


def native_notice(e):
    if platform.system()!='Darwin':return False
    msg = ('盘中提醒测试' if e.get('test') else
           f"{e['name']} {e['kind']}：价格/回撤 {e['price']}；参考位 {e['level']}，请核对券商行情")
    applescript='display notification '+json.dumps(msg,ensure_ascii=False)+' with title "A股盘中监控：非委托"'
    if e.get('sound') is True:applescript+=' sound name "Glass"'
    try:
        subprocess.run(['osascript','-e',applescript],check=True,timeout=8,capture_output=True)
        return True
    except subprocess.TimeoutExpired:return None
    except (OSError,subprocess.SubprocessError):return False


def journal_diagnostics(cfg, quotes, snapshot, active_signals, now):
    """Archive relevant V1 gates when a prepared candidate reaches its trigger."""
    if not strategy.enabled(cfg):
        return []
    from strategy_setup import evaluate_entry
    dates = snapshot.get('trading_dates') or []
    today = str(now.date())
    if today not in dates or dates.index(today) == 0:
        return []
    previous = dates[dates.index(today) - 1]
    active = {row['code'] for row in active_signals if row.get('trigger') == 'v1_entry'}
    blocked = []
    for code, plan in cfg.get('trade_plans', {}).items():
        quote = quotes.get(code, {})
        price = valid_price(quote.get('price'))
        trigger = valid_price(plan.get('trigger_price'))
        if (code in active or quote.get('held') or not price or not trigger or price < trigger
                or plan.get('signal_date') != previous or plan.get('setup_verified') is not True):
            continue
        result = evaluate_entry(cfg, code, quote, snapshot, now=now)
        if not result['blockers']:
            continue
        key = f'{today}|entry_blocked|{code}|{trigger}|{result["blockers"]}'
        blocked.append({
            'id': hashlib.sha256(key.encode()).hexdigest()[:20], 'code': code,
            'name': quote.get('name', code), 'title': 'V1新增条件受阻', 'trigger': 'v1_entry',
            'category': 'diagnostic', 'price': price, 'level': trigger,
            'reason': '已进入计划观察范围，继续等待条件复核。',
            'blockers': result['blockers'],
        })
    return blocked


def single_cycle(*,mode,notify=False,force_demo=False,probe=False,interval=10):
    cycle_started_monotonic=time.monotonic()
    now=now_cn();cfg=load_config(ROOT/'config.json')
    demo=(mode=='demo');market_open=in_session(now)
    cal_ok,cal_msg=verified_trading_date(now.date(),demo)
    auction_phase=current_auction_phase(now)
    preopen=(auction_phase!='closed')
    # Lunch/after-close polling runs only at the normal 5-minute rest cadence.
    # It refreshes a display-only snapshot, never trading capacity or alerts.
    minute=now.hour*60+now.minute
    reference_only=bool(not demo and cal_ok and not market_open and not preopen and
                        (690<=minute<780 or minute>=900))
    should_fetch=demo or ((market_open or preopen) and cal_ok) or probe or reference_only
    cfg_hash=hashlib.sha256(json.dumps(cfg,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    state={'mode':mode,'market_time':now.isoformat(timespec='seconds'),'updated_at':dt.datetime.now(dt.timezone.utc).isoformat(),
           'config_hash':cfg_hash,
           'interval_seconds':interval,'market_session':bool(market_open),'calendar_ok':cal_ok,
           'desktop_notifications_enabled':bool(notify and not demo and not probe and platform.system()=='Darwin'),
           'calendar_info':cal_msg,'source_timestamp_verified':False,
           'data_source':'演示模拟' if demo else QUOTE_SOURCE,
           'status':'休市或非交易时段，已暂停抓取','quotes':[],'alerts':[],'risk':None,'errors':[],
           'trade_signals':[],'new_trade_signals':[],'recent_trade_signals':[],
           'warning':'仅参考提醒、不自动交易；数据可能延迟、冻结或缺失；本地轮询并非交易所级实时行情。'}
    if SIGNAL_HISTORY.exists():
        try:state['recent_trade_signals']=[json.loads(x) for x in
            SIGNAL_HISTORY.read_text(encoding='utf-8').splitlines()[-80:] if x.strip()]
        except (OSError,ValueError):pass
    if not should_fetch:
        if market_open and not cal_ok: state['status']='交易日历核验失败，已暂停行情和提醒'
        store_json(STATUS_FILE,state)
        return state
    # Slow overseas context never blocks prioritized portfolio quotes.
    # Indicative auction prices are not used for risk estimates.
    if not demo and (preopen or market_open):
        refresh_context_async(cfg,now)
    elif demo:
        store_json(CONTEXT_FILE,{'mode':'demo','phase':auction_phase,'status':'模拟数据',
            'auction':{'rows':[]},'global':{'rows':[]},'generated_at':now.isoformat(),
            'warning':'演示数据禁止交易'})
    if preopen and not demo:
        state['status']='集合竞价参考数据已抓取；不参与持仓风险计算或触发止损提醒'
        state['data_source']='AKShare盘前分钟数据（非官方成交行情）'
        state['market_session']=False
        state['market_phase']=auction_phase
        store_json(STATUS_FILE,state)
        return state
    prior_state=read_json(STATUS_FILE,{})
    previous_cycle_time=prior_state.get('market_time')
    if previous_cycle_time:
        try:
            last=dt.datetime.fromisoformat(previous_cycle_time)
            gap=(now-last.astimezone(CST)).total_seconds()
            state['actual_cycle_gap_seconds']=round(gap,2) if 0<=gap<3600 else None
        except (ValueError,TypeError):state['actual_cycle_gap_seconds']=None
    # Hold every poll; watch-only names are lower priority and refreshed ~once/minute.
    prev_watch=prior_state.get('watchlist_fetched_at')
    try:
        watch_due=not prev_watch or (now-dt.datetime.fromisoformat(prev_watch).astimezone(CST)).total_seconds()>=60
    except (TypeError,ValueError):watch_due=True
    if demo:feed,errors=example_quotes(cfg)
    else:feed,errors=snapshot_selected_akshare(cfg,include_watch=watch_due)
    state['watchlist_fetched_at']=now.isoformat() if watch_due else prev_watch
    state['watchlist_refresh_seconds']=60
    health=assess(cfg,feed,prior_state if prior_state.get('config_hash')==cfg_hash else {},
                  dt.datetime.now(dt.timezone.utc))
    if demo:
        health['block_new_signals']=True
        health['block_trade_signals']=True
    state['quote_health']=health
    state['source_timestamp_verified']=health['provider_timestamps_verified']
    # Withhold unreasonable jump prints from calculations; no stale-price fallback.
    if health['invalid_jump']:
        for position in cfg.get('positions',[]):
            code=code_str(position['code'])
            if code in health['invalid_jump']:feed.pop((position['type'],code),None)
    state['errors']=errors
    account={}
    if strategy.enabled(cfg) and not demo and not probe:
        strategy.refresh_evidence_async(OUT,cfg)
        verified=bool(market_open and cal_ok and health['all_held_usable'] and not health['block_trade_signals'])
        equity=float(cfg.get('cash',0))+sum(float(feed[(p['type'],code_str(p['code']))][0])*int(p['quantity'])
                  for p in cfg.get('positions',[]) if int(p.get('quantity',0))>0 and (p['type'],code_str(p['code'])) in feed)
        try:account=strategy.observe(OUT,cfg,equity,verified=verified)
        except Exception as exc:
            account={'initialized':False,'verified':False,'needs_reconcile':True,'error':'V1记录核验失败：'+type(exc).__name__}
            state['errors'].append(account['error'])
    quotes,events,risk=evaluate(cfg,feed,demo=demo,strategy_account=account)
    if strategy.enabled(cfg):
        from strategy_setup import exit_review
        dates=strategy.calendar_dates(OUT)
        prepared=strategy.read(OUT/'strategy_prepared.json')
        for code,quote in quotes.items():
            plan=cfg.get('trade_plans',{}).get(code,{})
            proof=prepared.get(plan.get('prepared_id'),{})
            if proof.get('code')==code:quote['strategy_setup']=proof.get('setup')
            position=next((p for p in cfg.get('positions',[]) if code_str(p['code'])==code and int(p.get('quantity',0))>0),None)
            if position:
                quote['strategy_exit']=exit_review(cfg,position,plan,quote.get('price'),
                     history=strategy.history_frame(strategy.evidence(OUT,code)),trading_dates=dates,now=now)
        state['strategy_account']=account
        state['trading_dates']=dates
    state['quotes']=list(quotes.values())
    state['risk']={'valid':risk['valid'],'equity':risk['equity'],'used':risk['used'],'remaining':risk['remaining'],
                   'existing_planned_stress':risk['existing_planned_stress'],'available_new_risk':risk['available_new_risk'],
                   'missing':risk['missing'],'pnl':risk['pnl'],
                   'budget':risk['budget'],'over_budget':risk['over_budget'],
                   'stop_breaches':risk['stop_breaches'],'status':risk['status'],
                   'positions':risk['positions'],'sizing':risk['sizing'],'alert_level':risk_level(risk),
                   'strategy':risk.get('strategy')}
    # Live trade-review signals are not order instructions. Only regular-session,
    # calendar-verified polling data can create popup events; auction/demo/probe cannot.
    state['fetch_finished_at']=dt.datetime.now(dt.timezone.utc).isoformat()
    active_signals=build_signals(cfg,quotes,risk,trading_day=now.date(),snapshot=state,now=dt.datetime.now(CST),
        market_session=market_open and not probe and not health['block_trade_signals'],calendar_ok=cal_ok,demo=demo)
    if t_runtime.settings(cfg)['enabled'] and not demo and not probe:
        t_runtime.refresh_minutes_async(OUT,cfg,now)
        state['t_observe']=t_runtime.observe(OUT,cfg,quotes,risk,now=dt.datetime.now(CST),
            market_session=market_open and not health['block_trade_signals'],calendar_ok=cal_ok)
        active_signals.extend(state['t_observe'].get('signals',[]))
    state['trade_signals']=active_signals
    seen=read_json(SIGNAL_SENT,{'date':str(now.date()),'ids':[]})
    seen_ids=set(seen.get('ids',[])) if seen.get('date')==str(now.date()) else set()
    fresh_signals=[x for x in active_signals if x['id'] not in seen_ids]
    if mode=='live' and not probe and market_open and cal_ok:
        if fresh_signals:
            store_json(SIGNAL_SENT,{'date':str(now.date()),'ids':sorted(seen_ids|{x['id'] for x in fresh_signals})})
            with SIGNAL_HISTORY.open('a',encoding='utf-8') as f:
                for x in fresh_signals:
                    item={**x,'at':now.isoformat(timespec='seconds')}
                    f.write(json.dumps(item,ensure_ascii=False)+'\n')
    else:
        fresh_signals=[]
    state['new_trade_signals']=[{**x,'at':now.isoformat(timespec='seconds')} for x in fresh_signals]
    if SIGNAL_HISTORY.exists() and mode=='live':
        try:state['recent_trade_signals']=[json.loads(line) for line in
            SIGNAL_HISTORY.read_text(encoding='utf-8').splitlines()[-80:] if line.strip()]
        except (OSError,ValueError):pass
    no_required_prices=not any(x['price'] is not None for x in quotes.values())
    state['status']=('演示数据，仅验证程序' if demo else '休市期间连接测试（无交易信号）' if probe and not market_open
                      else '行情接口异常或无价格，暂停提醒' if no_required_prices
                      else '持仓报价缺失：'+ '、'.join(health['missing'])+'，暂停本轮新信号' if health['missing']
                      else '报价异常/停滞疑似，暂停本轮新信号' if health['block_new_signals']
                      else '源报价时间未全部核验：仅显示账户估算，暂停新交易提醒' if health['block_trade_signals']
                      else '盘中持仓已抓取；源报价时间戳已逐只验证（非交易所逐笔实时）')
    journal_clock=now_cn()
    if (mode=='live' and not probe and market_open and cal_ok and in_session(journal_clock)
            and journal_clock.date()==now.date()):
        try:
            recording_signals = []
            recording_events = []
            if not no_required_prices and not health['block_trade_signals']:
                recording_events = events
                recording_signals = active_signals + journal_diagnostics(cfg, quotes, state, active_signals, journal_clock)
            else:
                gaps = ['、'.join(health.get(key, [])) for key in
                        ('missing', 'stale_source', 'unverified_source', 'invalid_jump', 'suspected_freeze')]
                affected = '、'.join(gap for gap in gaps if gap)
                key = f'{now.date()}|quote_pause|{state["status"]}|{affected}'
                recording_signals = [{
                    'id': hashlib.sha256(key.encode()).hexdigest()[:20], 'code': 'PORT',
                    'name': '全账户', 'title': '行情暂停', 'trigger': 'quote_pause',
                    'category': 'diagnostic', 'reason': state['status'],
                    'blockers': [f'待核验证券 {affected}'] if affected else [],
                }]
            archived = persist_cycle(
                STATUS_FILE.parent,
                capture_rows(recording_events, recording_signals, quotes, cfg.get('trade_plans', {}),
                             now=journal_clock, source=state['data_source'], config_hash=cfg_hash),
                now=journal_clock, notify=notify and not health['block_trade_signals'],
                send=lambda row:native_notice({**row,'sound':row.get('repeatable') is True and (cfg.get('execution_preferences') or {}).get('native_sound') is True}),
                clock=now_cn, trades=cfg.get('reconciled_trades',[]),
            )
            if archived['attempted']:
                state['notifications_attempted']=archived['attempted']
                state['notifications_submitted']=archived['submitted']
        except (OSError, ValueError) as exc:
            state['errors'].append(f'提醒记录保存失败：{exc}')
    if demo or probe or not(market_open and cal_ok) or no_required_prices or health['block_trade_signals']:
        events=[]
    if mode=='live' and not probe and market_open and cal_ok and events:
        events=dedup_events(events,str(now.date()),demo=False)
    else:
        events=[] if not demo else events
    state['alerts']=events
    if events and not demo:
        OUT.mkdir(exist_ok=True)
        with HISTORY_FILE.open('a',encoding='utf-8') as f:
            for e in events:
                e['at']=now.isoformat(timespec='seconds')
                f.write(json.dumps(e,ensure_ascii=False)+'\n')
    recent=[]
    if not demo and HISTORY_FILE.exists():
        try:
            recent=[json.loads(x) for x in HISTORY_FILE.read_text(encoding='utf-8').splitlines()[-20:] if x.strip()]
        except (OSError,ValueError):pass
    state['recent_alerts']=recent
    state['fetch_finished_at']=dt.datetime.now(dt.timezone.utc).isoformat()
    state['updated_at']=state['fetch_finished_at']
    if not demo and not probe and market_open and cal_ok and risk['valid'] and health['all_held_usable'] and not health['block_new_signals']:
        record_equity(EQUITY_JOURNAL,at=state['fetch_finished_at'],config_hash=cfg_hash,
                      amount=risk['equity'],source_status='公开行情源时间戳已核验' if health['provider_timestamps_verified'] else '公开行情快照，源时间戳未核验')
    state['fetch_duration_seconds']=round(time.monotonic()-cycle_started_monotonic,2)
    state['source_freshness_note']='本机采集完成≠行情源时间。未核验源时间时仅供估算、禁止新交易信号。'
    if not demo and not probe:
        saved=save_display_snapshot(STATUS_FILE.with_name('last_account_snapshot.json'),cfg,state,
                                    historical=reference_only)
        if reference_only:
            state['status']=('休市参考报价已获取；保留账户估算，不触发交易提醒' if saved
                             else '休市报价未完整核验；保留最近完整快照')
            state['risk']['valid']=False
            state['risk']['available_new_risk']=None
            state['risk']['sizing']=[]
            state['risk']['status']='休市参考估值，不用于新增交易额度'
    store_json(STATUS_FILE,state)
    return state


def main():
    ap=argparse.ArgumentParser(description='A股盘中轮询提醒，仅本机提示，禁止自动下单')
    ap.add_argument('--mode',choices=('live','demo'),default='live')
    ap.add_argument('--interval',type=int,default=10,help='盘中目标采集间隔（秒），默认10；不保证报价新鲜度')
    ap.add_argument('--once',action='store_true',help='仅执行一轮，适合诊断')
    ap.add_argument('--probe',action='store_true',help='休市期间尝试联网测试，禁用提醒')
    ap.add_argument('--notify',action='store_true',help='在Mac发送本地通知')
    a=ap.parse_args()
    if not 10<=a.interval<=1800:ap.error('间隔必须为10—1800秒')
    OUT.mkdir(exist_ok=True)
    with LOCK_FILE.open('w') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            print('已有盘中监控进程在运行；避免重复发送提醒',file=sys.stderr);sys.exit(2)
        print('行情服务：AKShare非交易所级实时；账户变化务必手工同步。Ctrl+C 停止。',flush=True)
        while True:
            cycle_started=time.monotonic()
            st={}
            try:
                st=single_cycle(mode=a.mode,notify=a.notify,probe=a.probe,interval=a.interval)
                print(st['market_time'],st['status'],'|新提醒:',len(st['alerts']),'|错误:',len(st['errors']),flush=True)
            except Exception as exc:
                store_json(STATUS_FILE,{'mode':a.mode,'updated_at':dt.datetime.now(dt.timezone.utc).isoformat(),
                    'status':f'监控异常，已停止本轮信号: {type(exc).__name__}: {exc}', 'quotes':[],'alerts':[],
                    'source_timestamp_verified':False,'risk':None,'errors':[str(exc)]})
                print('轮询失败：',repr(exc),file=sys.stderr,flush=True)
            if a.once:break
            # Aim for 10s cycle starts rather than sleeping 10s *after* a potentially
            # slow public-data fetch. This is a best-effort polling interval, not quote latency.
            # Do not poll continuously during lunch, overnight, holidays or weekends.
            t=now_cn()
            phase=current_auction_phase(t)
            if phase!='closed' or in_session(t):
                pause=max(1.0,a.interval-(time.monotonic()-cycle_started))
                current_risk=st.get('risk') or {}
                if not current_risk.get('valid',False):
                    pause=max(pause,30)  # Backoff on incomplete data/errors to reduce source pressure
            else:
                pause=max(a.interval,300)
            time.sleep(pause)


if __name__=='__main__':main()
