"""Local-only A-share research dashboard. No broker API, no trade execution."""
from __future__ import annotations
import argparse
import csv
import hashlib
import hmac
import json
import math
import re
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone, time as clock_time
from zoneinfo import ZoneInfo
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file
from app import code_str, load_config, stock_history, score_stock
from risk_engine import compute, risk_prefs
from trade_levels import make_levels, validate_plan
from chan_history_engine import analyze_chan_history
from asset_history import history_by_asset
from technical_engine import analyze_technical, combine_chan_technical
from signal_review import inspect as inspect_signal
from swing_analysis import analyze_swing
from portfolio_quotes import snapshot_selected_akshare, snapshot_selected_tencent
from close_review import build_close_review
from quote_health import parse_provider_time
from risk_extensions import simulate_reduction
from portfolio_journal import read_lines,log_manual_trade,daily_curve
from account_snapshot import load_display_snapshot
from broker_snapshot import latest_broker_snapshot
import strategy_runtime as strategy
import t_runtime as t_runtime
from suggested_exit import build_exit_suggestion

ROOT = Path(__file__).resolve().parent
CSRF = secrets.token_urlsafe(32)
_EXIT_SECRET=secrets.token_bytes(32)
WRITE_LOCK = threading.RLock()
JOB_LOCK = threading.Lock()
JOB = {'running':False,'message':'尚未在本次网页会话启动扫描','started':None,'finished':None,'returncode':None}
ALLOWED_MODES = {'live','demo'}
MARKET_CODE = re.compile(r'^(?:00|30|60)\d{4}$')
ETF_CODE = re.compile(r'^(?:15|51|56|58)\d{4}$')


def clean_number(x, lower=None, upper=None, integer=False):
    n=float(x)
    if not math.isfinite(n) or (lower is not None and n<lower) or (upper is not None and n>upper):
        raise ValueError('数值超出允许范围')
    if integer:
        if n != int(n): raise ValueError('数量必须是整数')
        return int(n)
    return n


def code_of(x):
    code=str(x).strip()
    if not (MARKET_CODE.fullmatch(code) or ETF_CODE.fullmatch(code)):
        raise ValueError('请输入沪深股票或ETF的6位证券代码')
    return code


def json_hash(cfg):
    return hashlib.sha256(json.dumps(cfg,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def safe_csv(path, limit=100):
    if not path.is_file(): return []
    rows=[]
    with path.open(encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            clean={}
            for key,value in row.items():
                if value is None or value in ('','nan','NaN','None'):
                    clean[key]=None
                else:clean[key]=value
            rows.append(clean)
            if len(rows)>=limit:break
    return rows


def create_app(folder:Path=ROOT):
    folder=Path(folder).resolve()
    app=Flask(__name__, template_folder=str(ROOT/'templates'), static_folder=str(ROOT/'static'))
    app.json.ensure_ascii=False
    confpath=folder/'config.json'
    output=folder/'output'

    def config():
        return load_config(confpath)

    def save_config(cfg):
        # Persist using an atomic replace to prevent partial reads by the scheduled scanner.
        load_config_values(cfg)
        path=confpath.with_suffix('.json.tmp')
        path.write_text(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        path.replace(confpath)

    def load_config_values(cfg):
        if cfg.get('baseline_equity',0)<=0 or cfg.get('max_additional_loss',0)<=0 or cfg.get('cash',-1)<0:
            raise ValueError('账户基准、市值预算必须为正，现金不能为负')
        for p in cfg.get('positions',[]):
            if p['type'] not in ('stock','etf','convertible') or p['quantity']<0 or p['baseline_price']<=0:
                raise ValueError('持仓台账异常')

    def exit_suggestion(cfg,code):
        position=next((p for p in cfg.get('positions',[]) if code_str(p['code'])==code and int(p.get('quantity',0))>0),None)
        if not position:raise ValueError('退出建议仅用于已有持仓；新开仓仍须核验V1形态和账户额度')
        evidence=strategy.evidence(output,code)
        if not isinstance(evidence,dict):evidence={}
        try:dates=strategy.calendar_dates(output)
        except (TypeError,AttributeError,ValueError):dates=[]
        try:history=strategy.history_frame(evidence)
        except (TypeError,AttributeError,ValueError):history=None
        result=build_exit_suggestion(history,position,trading_dates=dates,existing_plan=cfg.get('trade_plans',{}).get(code))
        result.update(code=code,name=position['name'])
        refresh_status=strategy.read(output/'strategy_evidence'/'status.json')
        refresh_info=refresh_status.get('by_code',{}).get(code,{}) if isinstance(refresh_status,dict) else {}
        result['refresh_status']={**refresh_info,'attempted_at':refresh_status.get('at') if isinstance(refresh_status,dict) else None}
        if not result.get('technical',{}).get('valid') and refresh_info.get('refresh_error'):
            problem=refresh_info['refresh_error']
            if 'RateLimit' in problem:problem='上游日线接口限流，等待重试'
            detail=f"缓存截至{refresh_info.get('asof') or '无数据'}，应更新至{refresh_info.get('expected_asof') or '待核验交易日'}；最近刷新失败：{problem}"
            if result.get('preserved'):result.setdefault('warnings',[]).append(detail)
            else:result['reason']=(result.get('reason') or '完整日线待更新')+'；'+detail
        if strategy.enabled(cfg) and (not result.get('valid') or not result.get('technical',{}).get('valid')):
            strategy.refresh_evidence_async(output,cfg)
        if result.get('valid') and result.get('adoptable') and not result.get('preserved'):
            # A private per-process key signs the current cache + account
            # configuration. The browser cannot turn an exit draft into entry
            # authority, or silently adopt a changed/older source after refresh.
            payload={'code':code,'config_hash':json_hash(cfg),'asof':result.get('asof'),
                     'plan':result.get('plan'),'evidence_hash':hashlib.sha256(json.dumps(evidence,sort_keys=True,ensure_ascii=False).encode()).hexdigest()}
            result['suggestion_token']=hmac.new(_EXIT_SECRET,json.dumps(payload,sort_keys=True,ensure_ascii=False).encode(),hashlib.sha256).hexdigest()
        return result

    def exit_suggestions(cfg):
        if not strategy.enabled(cfg):return {}
        return {code_str(p['code']):exit_suggestion(cfg,code_str(p['code']))
                for p in cfg.get('positions',[]) if int(p.get('quantity',0))>0}

    @app.route('/api/exit-suggestions')
    def all_exit_suggestions():
        with WRITE_LOCK:cfg=config()
        return jsonify(suggestions=exit_suggestions(cfg),status='完整日线技术观察与剩余仓位退出草案；采用前不是已锁定止损，不产生新增额度')

    @app.route('/api/exit-suggestion/<code>')
    def get_exit_suggestion(code):
        if not re.fullmatch(r'\d{6}',code):raise ValueError('请输入6位持仓代码')
        with WRITE_LOCK:cfg=config()
        return jsonify(exit_suggestion(cfg,code))

    @app.route('/api/exit-suggestion/<code>/adopt',methods=['POST'])
    def adopt_exit_suggestion(code):
        if not re.fullmatch(r'\d{6}',code):raise ValueError('请输入6位持仓代码')
        body=request.get_json(silent=True) or {}
        with WRITE_LOCK:
            cfg=config();suggestion=exit_suggestion(cfg,code)
            if suggestion.get('preserved'):
                return jsonify(ok=True,preserved=True,message='已有固定退出计划已保留；不会用最新建议覆盖原止损和目标。')
            if not suggestion.get('adoptable'):
                raise ValueError(suggestion.get('reason') or '当前草案不适合采用，请先复核风险或等待近端结构')
            token=str(body.get('suggestion_token',''))
            if not suggestion.get('valid') or not token or not hmac.compare_digest(token,suggestion.get('suggestion_token','')):
                raise ValueError('建议数据已变更、过期或尚未核验，请点击“刷新系统建议”后再采用')
            proposed=suggestion['plan'];fixed=validate_plan(proposed)
            fixed=strategy.validate_metadata(output,cfg,code,{'legacy_exit_only':True,'sector':body.get('sector',proposed.get('sector',''))},fixed)
            for key in ('source_asof','reference_kind','suggestion_method'):
                if key in proposed:fixed[key]=proposed[key]
            fixed.update(strategy_version='1.0',legacy_exit_only=True,setup_verified=False)
            with strategy.locked(output):
                revisions=strategy.read(output/'strategy_plan_revisions.json',[])
                revisions.append({'code':code,'at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'before':cfg.get('trade_plans',{}).get(code),'after':fixed,'source':'采用系统退出建议'})
                strategy.write(output/'strategy_plan_revisions.json',revisions)
            cfg.setdefault('trade_plans',{})[code]=fixed;save_config(cfg)
        return jsonify(ok=True,plan=fixed,message='已锁定系统建议退出价；不下单、不补造买入日期，不据此放开新增额度。')

    @app.before_request
    def check_security():
        # Defensive checks for local browser apps (including DNS rebinding / CSRF).
        if request.host not in ('127.0.0.1:8765','localhost:8765','127.0.0.1','localhost'):
            return jsonify(error='无效Host。仅支持本机访问'),403
        if request.method in ('POST','PUT','PATCH','DELETE'):
            origin=request.headers.get('Origin')
            if origin and origin not in ('http://127.0.0.1:8765','http://localhost:8765'):
                return jsonify(error='跨站操作已拒绝'),403
            if not secrets.compare_digest(request.headers.get('X-CSRF-Token',''),CSRF):
                return jsonify(error='安全校验失败，请刷新页面',code='csrf_expired'),403

    @app.after_request
    def headers(res):
        res.headers['Cache-Control']='no-store'
        res.headers['X-Content-Type-Options']='nosniff'
        res.headers['X-Frame-Options']='DENY'
        res.headers['Referrer-Policy']='no-referrer'
        res.headers['Content-Security-Policy']="default-src 'self'; img-src 'self' data:; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'"
        return res

    @app.errorhandler(ValueError)
    def validation_error(exc):return jsonify(error=str(exc)),400

    @app.route('/')
    def index(): return render_template('dashboard.html',csrf=CSRF)

    @app.route('/paper-contest')
    def paper_contest_page():
        return render_template('paper_contest.html')

    @app.route('/api/paper-contest')
    def api_paper_contest():
        from paper_view import load_snapshot
        try:return jsonify(load_snapshot(output/'paper_contest_20261113_20261119'))
        except FileNotFoundError:return jsonify(error='尚未建立独立模拟账本；不会使用实盘配置代替。'),404
        except (ValueError,KeyError,TypeError,OverflowError):return jsonify(error='模拟账本校验未通过，暂不展示。'),409

    @app.route('/api/paper-contest/refresh',methods=['POST'])
    def refresh_paper_contest():
        from paper_runtime import refresh_store
        from paper_view import load_snapshot
        store=output/'paper_contest_20261113_20261119'
        result=refresh_store(store)
        if result['status']=='missing':return jsonify(error=result['message']),404
        try:return jsonify(snapshot=load_snapshot(store),refresh=result)
        except (ValueError,KeyError,TypeError,OverflowError):return jsonify(error='模拟账本校验未通过，暂不展示。'),409

    @app.route('/api/session')
    def session_token():
        # Same-origin JSON only. No CORS: third-party pages cannot read tokens.
        # Keep the Host/Origin/Fetch-Metadata checks rather than disabling CSRF.
        if (request.headers.get('Sec-Fetch-Site')=='cross-site' or
            request.headers.get('Origin') not in (None,'http://127.0.0.1:8765','http://localhost:8765')):
            return jsonify(error='跨站请求已拒绝'),403
        return jsonify(csrf_token=CSRF)

    @app.route('/api/state')
    def api_state():
        with WRITE_LOCK: cfg=config()
        metadata={}
        try:metadata=json.loads((output/'run_meta.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):pass
        scan_ok=metadata.get('scan_ok',True)
        fresh=False
        minutes_old=None
        reason='尚未运行，或者报告没有来源标识'
        if metadata:
            stamp=metadata.get('generated_at')
            try:
                dt=datetime.fromisoformat(stamp)
                if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
                minutes_old=max(0.0,(datetime.now(timezone.utc)-dt.astimezone(timezone.utc)).total_seconds()/60)
            except (TypeError, ValueError):pass
            if not scan_ok:reason='本次扫描失败，旧报告已停用：'+str(metadata.get('failure') or '请重新执行联网筛选')[-300:]
            elif metadata.get('mode')!='live':reason='当前报告为演示数据，不能用于交易'
            elif metadata.get('config_hash')!=json_hash(cfg):reason='修改了设置或持仓，请重新运行真实行情'
            elif minutes_old is None or minutes_old>20:reason='快照超过20分钟，请重新扫描'
            else:
                fresh=True
                reason='仅代表近期抓取快照，未核验交易所时间戳'
        qrows=safe_csv(output/'watch_quotes.csv',6000) if metadata.get('mode')=='live' and scan_ok else []
        # Watch quotes come from the same complete market snapshot as the report.
        lookup={r['code']:r for r in qrows if r.get('code')}
        watches=[]
        for p in cfg.get('watchlist',[]):
            code=code_str(p['code']); quote=lookup.get(code,{}) if fresh else {}
            price=quote.get('price')
            try:price=float(price) if price is not None else None
            except (ValueError,TypeError):price=None
            holding=next((x for x in cfg.get('positions',[]) if code_str(x['code'])==code and int(x.get('quantity',0))>0),None)
            plan=make_levels(cfg,code,price=price,position=holding,asset_type=holding['type'] if holding else p.get('type','stock'),price_label='行情快照价')
            watches.append({'code':code,'name':p['name'],'price':price,
                            'asset_type':p.get('type','stock'),
                            'pct':quote.get('pct'),'amount':quote.get('amount'),
                            'volume_ratio':quote.get('volume_ratio'),'levels':plan})
        pdata=safe_csv(output/'portfolio.csv',500) if fresh and scan_ok else []
        quotes={}
        for item in pdata:
            try:
                x=float(item['当前快照价'])
                if x>0 and math.isfinite(x):quotes[item['代码']]=x
            except (ValueError,TypeError,KeyError):pass
        previous_closes={}
        for item in pdata:
            try:
                from pnl_engine import valid_price
                prev=valid_price(item.get('昨收参考'))
                if prev is not None: previous_closes[item['代码']]=prev
            except (TypeError, ValueError, KeyError):
                pass
        # The scan is a research snapshot. Only the intraday monitor validates
        # the market session and the source time needed for new-entry sizing.
        account_context={**strategy.get_account(output,cfg),'verified':False}
        risk=compute(cfg,quotes,fresh=fresh,previous_closes=previous_closes,
                     strategy_account=account_context)
        risk['available_new_risk']=None
        # Sizing needs a valid account state, plus current quote for that particular watch.
        result={'config':cfg,'watchlist':watches,'risk':risk,'meta':metadata,
                'exit_suggestions':exit_suggestions(cfg),
                'display_snapshot':load_display_snapshot(output/'last_account_snapshot.json',cfg),
                'broker_snapshot':latest_broker_snapshot(output,cfg),
                'fresh':fresh,'fresh_reason':reason,'minutes_old':round(minutes_old,1) if minutes_old is not None else None,
                'scan_ok':scan_ok,'scan_failure':metadata.get('failure'),
                'latest_completed_day':strategy.latest_completed_day(output),
                'top':safe_csv(output/'verified_top.csv',30) if scan_ok else [],
                'candidates':safe_csv(output/'market_candidates.csv',30) if scan_ok else [],
                'sectors':safe_csv(output/'sector_rank.csv',25) if scan_ok else [],
                'alerts':safe_csv(output/'alerts.csv',50) if scan_ok else [],
                'job':scan_job_state()}
        return jsonify(result)

    @app.route('/api/intraday')
    def intraday_state():
        """Show external polling worker state. Never claim it is a live exchange feed."""
        try:
            state=json.loads((output/'intraday_status.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):
            return jsonify(status='尚未启动盘中监控',stale=True,quotes=[],alerts=[],risk=None,
                           warning='请在终端运行 intraday_monitor.py，或安装 macOS 登录任务')
        try:
            updated=datetime.fromisoformat(state.get('fetch_finished_at') or state['updated_at'])
            if updated.tzinfo is None:raise ValueError('必须有时区')
            age=max(0,(datetime.now(timezone.utc)-updated).total_seconds())
            state['seconds_since_update']=int(age)
            max_age=max(45,int(state.get('interval_seconds',10))*4) if state.get('market_session') else 660
            state['stale']=bool(age>max_age)
        except (KeyError,TypeError,ValueError):
            state['stale']=True
        # Polling results must match the account ledger used by the current webpage.
        # Editing a position or cash invalidates old P&L until a fresh poll finishes.
        try:
            with WRITE_LOCK: current_cfg=config()
            state['display_snapshot']=load_display_snapshot(output/'last_account_snapshot.json',current_cfg)
            state['broker_snapshot']=latest_broker_snapshot(output,current_cfg)
            state['exit_suggestions']=exit_suggestions(current_cfg)
            if t_runtime.settings(current_cfg)['enabled']:
                book=t_runtime.ledger(output,current_cfg)
                if (state.get('t_observe') or {}).get('ledger_revision')!=book.get('revision'):
                    for key in ('trade_signals','new_trade_signals'):
                        state[key]=[x for x in state.get(key,[]) if x.get('category')!='t']
            if state.get('config_hash')!=json_hash(current_cfg):
                state['config_mismatch']=True
                state['stale']=True
                state['status']='账户配置已改变，等待下一轮行情采集后重新计算；旧风险数值已停用'
                state['risk']=None
                state['trade_signals']=[]
                state['new_trade_signals']=[]
        except Exception as exc:
            state['stale']=True
            state['risk']=None
            state['status']=f'无法验证账户配置：{type(exc).__name__}'
        if state['stale']:
            state['trade_signals']=[]
            state['new_trade_signals']=[]
            if not state.get('config_mismatch') and not state['status'].startswith('无法验证'):
                state['status']='监控数据已过期；请检查后台进程、网络或电脑休眠'
        return jsonify(state)

    def current_risk_snapshot():
        with WRITE_LOCK:cfg=config()
        try:state=json.loads((output/'intraday_status.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):raise ValueError('当前没有有效的盘中持仓快照')
        try:
            stamp=datetime.fromisoformat(state.get('fetch_finished_at') or state['updated_at'])
            age=(datetime.now(timezone.utc)-stamp.astimezone(timezone.utc)).total_seconds()
        except (KeyError,ValueError,TypeError):age=1e9
        if not(0<=age<=45 and state.get('mode')=='live' and state.get('calendar_ok') and state.get('market_session') and
               state.get('config_hash')==json_hash(cfg) and (state.get('risk') or {}).get('valid') and
               state.get('quote_health',{}).get('all_held_usable') and not state.get('quote_health',{}).get('block_new_signals')):
            raise ValueError('行情过期、异常、账户配置变动或不在有效盘中，暂停减仓测算')
        prices={x['code']:x['price'] for x in state.get('quotes',[]) if x.get('held') and x.get('price') is not None}
        return cfg,prices

    @app.route('/api/reduction',methods=['POST'])
    def reduce_preview():
        body=request.get_json(silent=True) or {}
        cfg,prices=current_risk_snapshot()
        code=str(body.get('code',''))
        if not re.fullmatch(r'\d{6}',code):raise ValueError('请输入6位持仓代码')
        quantity=clean_number(body.get('quantity'),1,100000000,integer=True)
        return jsonify(simulate_reduction(cfg,prices,code,quantity,strategy_account=strategy.get_account(output,cfg)))

    @app.route('/api/strategy')
    def strategy_state():
        cfg=config();result=strategy.status(output,cfg)
        live=strategy.read(output/'intraday_status.json')
        if live.get('config_hash')==json_hash(cfg):
            risk=live.get('risk') or {}
            result['risk']=risk.get('strategy')
            result['blockers']=list(dict.fromkeys(result['blockers']+(risk.get('strategy') or {}).get('blockers',[])))
        return jsonify(result)

    @app.route('/api/strategy/settings',methods=['PUT'])
    def strategy_settings():
        body=dict(request.get_json(silent=True) or {})
        for alias,key in [('fee_reserve','min_fees'),('account_risk_budget','loss_budget')]:
            if alias in body:body[key]=body[alias]
        with WRITE_LOCK:
            cfg=config();values=strategy.validate_settings({**strategy.settings(cfg),**body})
            cfg.setdefault('strategy_v1',{}).update(values)
            # Canonical aliases are kept equal for old clients and the risk engine.
            cfg['strategy_v1'].update(fee_reserve=values['min_fees'],account_risk_budget=values['loss_budget'])
            save_config(cfg)
        return jsonify(ok=True,message='V1约束已保存，等待下一轮真实行情复核。')

    @app.route('/api/strategy/cashflows',methods=['POST'])
    def strategy_flow():
        with WRITE_LOCK:entry=strategy.add_flow(output,config(),request.get_json(silent=True) or {})
        return jsonify(ok=True,flow=entry,message='外部转账已登记，请同步现金/持仓后核对；高水位不会重置。')

    @app.route('/api/strategy/reconcile',methods=['POST'])
    def strategy_reconcile():
        with WRITE_LOCK:
            cfg,prices=current_risk_snapshot()
            state=strategy.read(output/'intraday_status.json')
            if state.get('quote_health',{}).get('block_trade_signals'):
                raise ValueError('行情源时间未核验，暂不能核对 V1 账户')
            equity=float(cfg['cash'])+sum(prices[str(p['code'])]*int(p['quantity']) for p in cfg['positions'] if int(p['quantity'])>0)
            account=strategy.observe(output,cfg,equity,verified=True,reconcile=True)
        return jsonify(ok=True,account=account,message='已按当前台账和已核验报价核对，高水位与历史回撤保留。')

    @app.route('/api/strategy/reviews',methods=['POST'])
    def strategy_review():
        with WRITE_LOCK:review=strategy.add_review(output,config(),request.get_json(silent=True) or {})
        return jsonify(ok=True,review=review,message='完整交易复盘已记录；亏损与违规按规则触发冷静期。')

    @app.route('/api/strategy/resume',methods=['POST'])
    def strategy_resume():
        with WRITE_LOCK:account=strategy.resume(output,config(),request.get_json(silent=True) or {})
        return jsonify(ok=True,account=account,message='预算耗尽锁定已复盘确认；历史高水位保留，其他暂停条件继续生效。')

    @app.route('/api/strategy/prepare/<code>',methods=['POST'])
    def strategy_prepare(code):
        if not re.fullmatch(r'\d{6}',code):raise ValueError('请输入6位证券代码')
        return jsonify(strategy.prepare_bounded(output,config(),code))

    @app.route('/api/t-observe')
    def t_observe():
        cfg=config();state=strategy.read(output/'intraday_status.json')
        payload=dict(state.get('t_observe') or {})
        book=t_runtime.ledger(output,cfg)
        try:
            at=datetime.fromisoformat(state.get('fetch_finished_at',''))
            age=(datetime.now(timezone.utc)-at.astimezone(timezone.utc)).total_seconds() if at.tzinfo else 1e9
        except (ValueError,TypeError):age=1e9
        valid=bool(0<=age<=45 and state.get('config_hash')==json_hash(cfg) and state.get('mode')=='live'
                   and state.get('market_session') and state.get('calendar_ok')
                   and payload.get('ledger_revision')==book.get('revision')
                   and (state.get('risk') or {}).get('valid')
                   and not state.get('quote_health',{}).get('block_trade_signals'))
        if not payload:
            payload={'rows':[],'signals':[],'status':'等待后台采集当前分钟数据后核验做T条件'}
        payload.update(enabled=t_runtime.settings(cfg)['enabled'],settings=t_runtime.settings(cfg),
                       cycles=book['cycles'][-30:],availability=book['availability'],stale=not valid)
        if not valid:
            payload['signals']=[]
            payload['status']='非有效盘中、行情过期或台账已变化；暂停做T提示，保留记录供核对'
            payload['rows']=[{**row,'quantity':None,'tradable':False,'status':'暂停提示',
                              'blockers':list(dict.fromkeys(row.get('blockers',[])+['当前盘中快照未核验']))} for row in payload.get('rows',[])]
        return jsonify(payload)

    @app.route('/api/t-availability',methods=['PUT'])
    def t_available():
        with WRITE_LOCK:entry=t_runtime.confirm_available(output,config(),request.get_json(silent=True) or {})
        return jsonify(ok=True,availability=entry,message='已记录今日券商可卖数量；仍须核对实际委托与T+1，不自动交易。')

    @app.route('/api/t-cycles',methods=['POST'])
    def t_sell():
        with WRITE_LOCK:cycle=t_runtime.record_sell(output,config(),request.get_json(silent=True) or {})
        return jsonify(ok=True,cycle=cycle,message='已登记实际做T卖出；请同步持仓与现金并对账。普通减仓不会自动登记为T。')

    @app.route('/api/t-cycles/<cycle_id>/buyback',methods=['POST'])
    def t_buyback(cycle_id):
        with WRITE_LOCK:cycle=t_runtime.record_buyback(output,config(),cycle_id,request.get_json(silent=True) or {})
        return jsonify(ok=True,cycle=cycle,message='已登记实际接回；请同步持仓与现金，今日新买份额不增加今日可卖数量。')

    @app.route('/api/t-cycles/<cycle_id>/close',methods=['POST'])
    def t_leave_cash(cycle_id):
        with WRITE_LOCK:cycle=t_runtime.close_cycle(output,config(),cycle_id,request.get_json(silent=True) or {})
        return jsonify(ok=True,cycle=cycle,message='本轮记录已结束，未接回部分保留为现金；不会追价买回。')

    @app.route('/api/trades',methods=['GET','POST'])
    def manual_trades():
        path=output/'manual_trade_log.jsonl'
        if request.method=='GET':return jsonify(rows=list(reversed(read_lines(path,200))),note='仅人工登记记录，不连接券商、不自动改变持仓或现金。')
        with WRITE_LOCK:
            entry=log_manual_trade(path,request.get_json(silent=True) or {})
        return jsonify(ok=True,trade=entry,message='成交记录已保存；仍需在持仓与现金台账中同步实际成交变化。')

    @app.route('/api/equity-curve')
    def equity_curve():
        rows=daily_curve(output/'equity_observations.jsonl')
        return jsonify(rows=rows,source='公开行情持仓估算快照，每日最后一次记录；非券商净值，交易和现金变动会影响曲线连续性。',
                       count=len(rows))

    @app.route('/api/market-context')
    def market_context():
        """Read-only display; never claim a quote is exchange-realtime without a source timestamp."""
        path=output/'market_context.json'
        try:d=json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError):return jsonify(status='还没有盘前或海外行情数据',stale=True,auction={'rows':[]},global_market={'rows':[]})
        try:
            t=datetime.fromisoformat(d['generated_at'])
            if t.tzinfo is None:raise ValueError('missing tz')
            age=(datetime.now(timezone.utc)-t.astimezone(timezone.utc)).total_seconds()
            d['seconds_since_update']=max(0,int(age))
            d['stale']=age>600
        except (KeyError,TypeError,ValueError):d['stale']=True
        # Do not serve old auction reference quotes as actionable, even if local file was retained.
        if d['stale']:
            for r in d.get('auction',{}).get('rows',[]):r['usable']=False
        return jsonify(d)

    @app.route('/api/daily-observe')
    def daily_observe():
        """Independent, auto-refreshed daily research queue; never mutates user watchlist."""
        with WRITE_LOCK: cfg=config()
        out_mode='demo'
        try:
            meta=json.loads((output/'run_meta.json').read_text(encoding='utf-8'))
            out_mode=meta.get('mode','demo')
        except (OSError, ValueError):meta={}
        if meta.get('scan_ok') is False:
            reason=meta.get('failure') or '扫描失败'
            return jsonify(shortlist=[],count=0,status=reason,mode=out_mode,asof=None,stale=True,
                           blockers=[reason],source_errors=[reason],excluded=cfg.get('daily_excludes',[]))
        path=output/('daily_observe_demo.json' if out_mode=='demo' else 'daily_observe.json')
        try:
            data=json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return jsonify(shortlist=[],count=0,status='尚未生成每日观察榜，请执行盘后扫描',mode=out_mode,asof=None,stale=True)
        if strategy.enabled(cfg) and data.get('strategy_version')!='1.0':
            return jsonify(shortlist=[],count=0,status='V1已启用，等待按固定3日回调转强规则重新扫描',mode=out_mode,
                           asof=None,stale=True,strategy_version='1.0',excluded=cfg.get('daily_excludes',[]),
                           disclaimer='旧版评分榜不作为V1候选；不凑足5只。')
        expected=strategy.latest_completed_day(output)
        asof=data.get('asof')
        if not asof and not data.get('shortlist') and meta.get('scan_ok') is True:
            asof=meta.get('verified_asof')
        stale=not expected or asof!=expected or data.get('mode')!='live'
        excluded={str(x).zfill(6) for x in cfg.get('daily_excludes',[])}
        rows=[x.copy() for x in data.get('shortlist',[]) if x.get('code') not in excluded]
        user_codes={code_str(x['code']) for x in cfg.get('watchlist',[])}
        for item in rows:
            item['in_watchlist']=item['code'] in user_codes
            item['can_buy_now']=False
            item['max_buy_shares']=None
            item['risk_note']='历史日线候选；不等于实时买入信号'
        status=('演示合成数据，禁止交易' if out_mode=='demo' else
                '最近交易日日线技术观察，非实时买入推荐' if not stale else
                '数据已过期或日期不一致，暂停作为今日候选')
        if strategy.enabled(cfg):status=(data.get('status','V1固定形态观察，须准备次日计划并人工核验')+
                                       ('；数据日期未核验/已过期，不可作为今日候选' if stale else ''))
        return jsonify(mode=out_mode,asof=asof,generated_at=data.get('generated_at'),
                       count=len(rows),shortlist=rows,stale=stale,status=status,
                       excluded=sorted(excluded),disclaimer=data.get('disclaimer','仅供研究，非自动下单'),
                       strategy_version=data.get('strategy_version'),blockers=data.get('blockers',[]),source_errors=data.get('source_errors',[]))

    @app.route('/close-review')
    def close_review_page():
        return render_template('close_review.html')

    @app.route('/api/close-review')
    def close_review_api():
        """Build a read-only after-close plan from one complete quote batch."""
        with WRITE_LOCK:
            cfg = config()
        snapshot = latest_broker_snapshot(output, cfg)
        clock = datetime.now(strategy.CST)
        expected = strategy.latest_completed_day(output, clock)
        quote_cfg = {'positions': (snapshot or {}).get('positions') or cfg.get('positions', [])}
        feed, errors = snapshot_selected_tencent(quote_cfg, include_watch=False, deadline_seconds=7.5)
        quotes = {}
        for (kind, code), quote in feed.items():
            quotes[code] = {'price': quote[0], 'name': quote[1], 'previous_close': quote[2],
                            'source_timestamp': quote[3]}
        daily = strategy.read(output/'daily_observe.json')
        scan_meta = strategy.read(output/'run_meta.json')
        candidates = []
        if expected and daily.get('mode')=='live' and daily.get('asof')==expected and scan_meta.get('scan_ok', True):
            candidates = [row for row in daily.get('shortlist', []) if row.get('asof')==expected]
        complete_codes = {str(x.get('code')).zfill(6) for x in (snapshot or {}).get('positions', [])}
        def closing_stamp(value):
            stamp=parse_provider_time(value)
            return stamp.astimezone(strategy.CST) if stamp is not None else None
        def matches_close(stamp):
            return bool(stamp and stamp<=clock and stamp.date().isoformat()==expected and stamp.hour>=15)
        account_stamp=closing_stamp((snapshot or {}).get('asof'))
        stamps=[closing_stamp(quotes.get(code,{}).get('source_timestamp')) for code in sorted(complete_codes)]
        if not expected:errors.append('尚未核验最近完整交易日')
        if not matches_close(account_stamp):errors.append('券商快照未覆盖最近收盘，请更新持仓和现金')
        if not all(matches_close(stamp) for stamp in stamps):errors.append('部分证券缺少最近交易日的收盘报价')
        data_verified = bool(snapshot and expected and not errors)
        known_stamps=[stamp for stamp in stamps if stamp is not None]
        asof = min(known_stamps).isoformat() if known_stamps else None
        return jsonify(build_close_review(cfg, snapshot, quotes, candidates,
                                          asof=asof, data_verified=data_verified,
                                          quote_errors=errors))

    @app.route('/api/daily-excludes',methods=['POST','DELETE'])
    def daily_exclude():
        body=request.get_json(silent=True) or {}
        code=code_of(body.get('code',''))
        with WRITE_LOCK:
            cfg=config()
            vals={str(x).zfill(6) for x in cfg.get('daily_excludes',[])}
            if request.method=='POST':vals.add(code)
            else:vals.discard(code)
            cfg['daily_excludes']=sorted(vals)
            save_config(cfg)
        return jsonify(ok=True,message='每日候选排除清单已更新；人工自选股没有被删除')

    @app.route('/api/watchlist',methods=['POST'])
    def add_watch():
        body=request.get_json(silent=True) or {}
        code=code_of(body.get('code',''))
        name=str(body.get('name','')).strip()
        if not name or len(name)>32:raise ValueError('填写1—32字的股票名称')
        with WRITE_LOCK:
            cfg=config()
            if any(code_str(p['code'])==code for p in cfg.get('watchlist',[])):raise ValueError('该股票已在自选池中')
            cfg.setdefault('watchlist',[]).append({'code':code,'name':name,'type':'etf' if ETF_CODE.fullmatch(code) else 'stock'})
            save_config(cfg)
        return jsonify(ok=True,message='已加入观察池，需重新扫描以更新行情')

    @app.route('/api/watchlist/<code>',methods=['DELETE'])
    def delete_watch(code):
        code=code_of(code)
        with WRITE_LOCK:
            cfg=config(); old=cfg.get('watchlist',[])
            cfg['watchlist']=[x for x in old if code_str(x['code'])!=code]
            if len(cfg['watchlist'])==len(old):raise ValueError('股票不在观察池中')
            save_config(cfg)
        return jsonify(ok=True,message='已删除自选分析股票；持仓与警报规则未修改')

    @app.route('/api/levels',methods=['PUT'])
    def update_levels():
        body=request.get_json(silent=True) or {}
        code=code_of(body.get('code',''))
        lo=body.get('below'); hi=body.get('above')
        lo=None if lo in ('',None) else clean_number(lo,0.001,100000)
        hi=None if hi in ('',None) else clean_number(hi,0.001,100000)
        if lo is not None and hi is not None and lo>=hi:raise ValueError('跌破价需低于突破价')
        with WRITE_LOCK:
            cfg=config()
            name=next((x['name'] for x in cfg.get('watchlist',[]) if x['code']==code),None)
            if not name:raise ValueError('请先加入自选池')
            levels=[x for x in cfg.get('alert_levels',[]) if code_str(x['code'])!=code]
            if lo is not None or hi is not None:
                entry={'code':code,'name':name}
                if lo is not None:entry['below']=lo
                if hi is not None:entry['above']=hi
                levels.append(entry)
            cfg['alert_levels']=levels; save_config(cfg)
        return jsonify(ok=True,message='已保存观察价位，非自动止损单')

    @app.route('/api/risk-settings',methods=['PUT'])
    def update_settings():
        body=request.get_json(silent=True) or {}
        v={}
        for k in ('stop_stock','stop_etf','stop_convertible'):
            v[k]=clean_number(body.get(k),0.5,30)
        for k in ('tp1_pct','tp2_pct'):
            v[k]=clean_number(body.get(k),1,100)
        if v['tp1_pct']>=v['tp2_pct']:
            raise ValueError('第一止盈百分比必须小于第二止盈百分比')
        v['per_trade_risk']=clean_number(body.get('per_trade_risk'),1,10000)
        v['max_position_pct']=clean_number(body.get('max_position_pct'),1,60)
        with WRITE_LOCK:old_settings=risk_prefs(config())
        v['min_reward_risk']=clean_number(body.get('min_reward_risk',old_settings['min_reward_risk']),1,10)
        v['max_breakout_extension_pct']=clean_number(body.get('max_breakout_extension_pct',old_settings['max_breakout_extension_pct']),0,10)
        with WRITE_LOCK:
            cfg=config()
            if v['per_trade_risk']>cfg['max_additional_loss']:raise ValueError('单笔计划损失不应高于账户预算')
            cfg['risk_settings']=v
            save_config(cfg)
        return jsonify(ok=True,message='规则已保存；重新扫描后才会给出当前买入数量')

    @app.route('/api/trade-plan',methods=['PUT'])
    def update_trade_plan():
        body=request.get_json(silent=True) or {}
        code=str(body.get('code','')).strip()
        if not re.fullmatch(r'\d{6}',code):raise ValueError('请输入6位证券代码')
        with WRITE_LOCK:
            cfg=config()
            held=any(code_str(p['code'])==code and int(p.get('quantity',0))>0 for p in cfg.get('positions',[]))
            raised=strategy.enabled(cfg) and held and float(body.get('tp1_sold_quantity') or 0)>0
            fixed=validate_plan(body,allow_raised_stop=raised) if strategy.enabled(cfg) else validate_plan(body)
            known=any(code_str(p['code'])==code for p in cfg.get('watchlist',[])) or any(code_str(p['code'])==code for p in cfg.get('positions',[]))
            if not known:raise ValueError('请先把股票加入观察池，或添加为持仓')
            if strategy.enabled(cfg):fixed=strategy.validate_metadata(output,cfg,code,body,fixed)
            if strategy.enabled(cfg):
                with strategy.locked(output):
                    revisions=strategy.read(output/'strategy_plan_revisions.json',[])
                    revisions.append({'code':code,'at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'before':cfg.get('trade_plans',{}).get(code),'after':fixed})
                    strategy.write(output/'strategy_plan_revisions.json',revisions)
            cfg.setdefault('trade_plans',{})[code]=fixed
            save_config(cfg)
        return jsonify(ok=True,message='固定止盈止损参考价已保存；仅是提醒，不会自动买卖。请重新运行真实行情扫描。')

    @app.route('/api/trade-plan/<code>',methods=['DELETE'])
    def reset_trade_plan(code):
        if not re.fullmatch(r'\d{6}',code):raise ValueError('请输入6位证券代码')
        with WRITE_LOCK:
            cfg=config()
            if strategy.enabled(cfg) and any(code_str(p['code'])==code and int(p.get('quantity',0))>0 for p in cfg.get('positions',[])):
                raise ValueError('仍有持仓时不能删除固定计划；请管理原退出计划，止损只允许上移')
            cfg.setdefault('trade_plans',{}).pop(code,None)
            save_config(cfg)
        return jsonify(ok=True,message='固定计划已移除；V1缺少固定计划时暂停新增额度，不生成模拟止损。')

    @app.route('/api/account',methods=['PUT'])
    def account():
        body=request.get_json(silent=True) or {}
        cash=clean_number(body.get('cash'),0,1e10)
        with WRITE_LOCK:
            cfg=config();cfg['cash']=cash;save_config(cfg)
        return jsonify(ok=True,message='现金已更新；请确认与券商一致，重新扫描行情')

    @app.route('/api/positions',methods=['POST','PUT'])
    def position():
        body=request.get_json(silent=True) or {}
        code=str(body.get('code','')).strip()
        if not re.fullmatch(r'\d{6}',code):raise ValueError('请输入6位证券代码')
        typ=body.get('type')
        if typ not in ('stock','etf','convertible'):raise ValueError('不支持的持仓类型')
        quantity=clean_number(body.get('quantity'),0,100000000,integer=True)
        cost=clean_number(body.get('cost'),0.001,1e7)
        baseline=clean_number(body.get('baseline_price'),0.001,1e7)
        name=str(body.get('name','')).strip()
        if not name or len(name)>36:raise ValueError('名称不能为空且不得超过36字')
        p={'code':code,'name':name,'type':typ,'quantity':quantity,'cost':cost,'baseline_price':baseline}
        sector=str(body.get('sector','')).strip()
        if len(sector)>40:raise ValueError('行业分类不应超过40字')
        if sector:p['sector']=sector
        with WRITE_LOCK:
            cfg=config();old=cfg.setdefault('positions',[])
            idx=next((i for i,x in enumerate(old) if x['code']==code),None)
            if request.method=='POST' and idx is not None:raise ValueError('该代码已经存在，请使用修改功能')
            if request.method=='PUT' and idx is None:raise ValueError('该代码不存在，请先添加')
            if idx is None:old.append(p)
            else:old[idx]={**old[idx],**p}
            save_config(cfg)
        return jsonify(ok=True,message='仅更新本地持仓台账，不代表在券商成交；请同步现金并重新扫描')

    @app.route('/api/positions/<code>',methods=['DELETE'])
    def delete_position(code):
        if not re.fullmatch(r'\d{6}',code):raise ValueError('错误代码')
        with WRITE_LOCK:
            cfg=config();old=cfg.get('positions',[])
            cfg['positions']=[x for x in old if x['code']!=code]
            if len(cfg['positions'])==len(old):raise ValueError('未找到持仓')
            save_config(cfg)
        return jsonify(ok=True,message='已从本地台账移除。请更新现金，否则账户净值会错误。')

    def scan_job_state():
        with JOB_LOCK:
            return dict(JOB)

    def run_scan(mode,top):
        try:
            args=[sys.executable,str(folder/'phase2.py'),'--mode',mode,'--top',str(top)]
            result=subprocess.run(args,cwd=str(folder),capture_output=True,text=True,timeout=1800)
            code=result.returncode
            msg=((result.stderr if code else result.stdout) or '').strip()[-1200:]
        except Exception as e:
            code=-1
            msg='扫描超时，请重试' if isinstance(e,subprocess.TimeoutExpired) else '无法启动扫描：'+str(e)
        if code not in (0,2,75):
            from phase2 import record_scan_failure
            try:
                record_scan_failure(output,mode,msg or '扫描失败')
            except OSError:
                app.logger.exception('无法保存扫描失败状态')
        with JOB_LOCK:
            JOB.update(running=False,finished=datetime.now(strategy.CST).isoformat(timespec='seconds'),
                       returncode=code,message=msg or ('扫描失败' if code else '扫描完成'))

    @app.route('/api/scan',methods=['POST'])
    def start_scan():
        body=request.get_json(silent=True) or {}
        mode=body.get('mode')
        if mode not in ALLOWED_MODES:raise ValueError('模式只允许live或demo')
        top=clean_number(body.get('top',30),1,150,integer=True)
        with JOB_LOCK:
            if JOB['running']:return jsonify(error='已有一个扫描正在运行'),409
            JOB.update(running=True,started=datetime.now(strategy.CST).isoformat(timespec='seconds'),
                       finished=None,returncode=None,message='扫描进行中…')
            queued=dict(JOB)
        threading.Thread(target=run_scan,args=(mode,top),daemon=True).start()
        return jsonify(ok=True,message='扫描任务已启动，网页会持续刷新状态',job=queued)

    @app.route('/api/job')
    def job_state():return jsonify(scan_job_state())

    @app.route('/api/analyze/<code>')
    def analyze(code):
        code=str(code).strip()
        if not re.fullmatch(r'\d{6}',code):raise ValueError('证券代码必须为6位数字')
        mode=request.args.get('mode','live')
        if mode not in ALLOWED_MODES:raise ValueError('无效分析模式')
        cfg=config()
        holding=next((x for x in cfg.get('positions',[]) if code_str(x['code'])==code and int(x.get('quantity',0))>0),None)
        watch=next((x for x in cfg.get('watchlist',[]) if code_str(x['code'])==code),None)
        if not MARKET_CODE.fullmatch(code) and not holding and not (watch and ETF_CODE.fullmatch(code)):
            raise ValueError('ETF及可转债仅支持已经录入持仓的代码')
        asset_type=holding['type'] if holding else (watch or {}).get('type','stock')
        name=next((x['name'] for x in cfg.get('watchlist',[]) if x['code']==code),holding['name'] if holding else code)
        now_cn=datetime.now(ZoneInfo('Asia/Shanghai'))
        window_start=now_cn.date()-timedelta(days=365)
        if mode=='demo':
            import numpy as np
            import pandas as pd
            rng=np.random.default_rng(int(code))
            closes=12+pd.Series(rng.normal(0.03,0.2,520)).cumsum()
            hist=pd.DataFrame({'date':pd.bdate_range(end=pd.Timestamp.today().normalize(),periods=520),
                               'open':closes,'close':closes,'high':closes+0.25,'low':closes-0.25,'volume':rng.integers(50000,180000,520)})
        else:
            begin=window_start.strftime('%Y%m%d')
            try:hist=history_by_asset(code,asset_type,begin)
            except Exception as e:return jsonify(error=f'真实日线获取失败，不能使用演示价格代替：{type(e).__name__} {e}'),502
        # Both a single chart and the batch holdings check use exactly one year.
        # Enforce the bound even if a provider ignores its requested start date.
        # Future bars and today's unfinished session never confirm a structure.
        import pandas as pd
        dates=pd.to_datetime(hist['date'],errors='coerce').dt.date
        cutoff=now_cn.date() if now_cn.time()>=clock_time(15,5) else now_cn.date()-timedelta(days=1)
        hist=hist.loc[dates.notna() & (dates>=window_start) & (dates<=cutoff)].copy().sort_values('date').reset_index(drop=True)
        analysis_window={'label':'最近1年','start':window_start.isoformat(),'end':cutoff.isoformat(),
                         'data_start':str(hist.iloc[0]['date'])[:10] if len(hist) else None,
                         'data_end':str(hist.iloc[-1]['date'])[:10] if len(hist) else None}
        chan=analyze_chan_history(hist)
        technical=analyze_technical(hist)
        integrated=combine_chan_technical(chan,technical)
        try:score=score_stock(code,name,hist)
        except Exception:
            # newly listed convertible bonds must still yield a truthful
            # insufficient-data result, not be replaced with imaginary prices.
            last=hist.iloc[-1] if len(hist) else None
            score={'日期':str(last.date.date()) if last is not None else '未知',
                   '收盘':round(float(last.close),3) if last is not None else None,
                   '涨跌幅%':None,'观察评分':'不可评分', 'MA5':None,'MA20':None,
                   '量比5日':None,'20日突破':'不可判定','说明':'日线数量不足，不评分'}
        history=[{'date':str(r.date.date()),'close':round(float(r.close),3)} for r in hist.itertuples()]
        levels=(make_levels(cfg,code,price=float(hist.iloc[-1]['close']),position=holding,
                           asset_type=asset_type,price_label='最新完整历史日线收盘价') if len(hist) else None)
        snapshot={};quote={};now_cn=datetime.now(ZoneInfo('Asia/Shanghai'))
        if mode=='live':
            try:snapshot=json.loads((output/'intraday_status.json').read_text(encoding='utf-8'))
            except (OSError,ValueError):pass
            if snapshot.get('config_hash')==json_hash(cfg):
                quote=next((q for q in snapshot.get('quotes',[]) if q['code']==code),{})
            source=parse_provider_time(quote.get('source_timestamp'))
            if not source or not 0<=(now_cn.astimezone(timezone.utc)-source).total_seconds()<=90:
                # One selected security, never request an all-market stock frame.
                try:
                    selected,errors=snapshot_selected_akshare({'positions':[{'code':code,'type':asset_type,'quantity':1}]},include_watch=False,deadline_seconds=4)
                    q=selected.get((asset_type,code))
                    quote={'price':q[0],'source_timestamp':q[3] if len(q)>3 else None} if q else {}
                    if q and len(q)>4 and isinstance(q[4],dict):quote.update(q[4])
                    if q and name==code and q[1]:name=q[1]
                except Exception:quote={}
        if strategy.enabled(cfg):snapshot['strategy_account']=strategy.get_account(output,cfg)
        swing=analyze_swing(hist,technical,chan,cfg,code,position=holding,quote=quote,
                            snapshot=snapshot,now=now_cn,mode=mode,
                            benchmark_hist=strategy.history_frame(strategy.evidence(output,'benchmark')),
                            trading_dates=strategy.calendar_dates(output))
        return jsonify({'name':name,'code':code,'asset_type':asset_type,'mode':mode,
                        'analysis_window':analysis_window,
                        'score':score,'history':history,'levels':levels,'chan':chan,
                        'technical':technical,'integrated':integrated,'swing':swing,'signal_review':inspect_signal(chan,technical,integrated),
                        'disclaimer':('演示合成数据，禁止交易' if mode=='demo' else
                                      ('真实历史日线；股票前复权、ETF/转债未复权；严格一二三类点仍需次级别确认；行情可能滞后。'))})

    @app.route('/report')
    def report():
        meta=strategy.read(output/'run_meta.json')
        if meta.get('scan_ok') is False:return '本次扫描未完成，请在筛选页面查看失败原因。',503
        path=output/'report_v2.html'
        if not path.is_file():return '尚未生成报告；请先运行扫描。',404
        return send_file(path, mimetype='text/html')

    return app


def main():
    arg=argparse.ArgumentParser(description='A股本地网页版研究面板（不下单）')
    arg.add_argument('--open',action='store_true',help='自动打开浏览器')
    args=arg.parse_args()
    if args.open:
        threading.Timer(1.2,lambda:webbrowser.open('http://127.0.0.1:8765')).start()
    print('A股量化网页： http://127.0.0.1:8765 （仅在本机访问；关闭终端会停止网页服务）')
    create_app().run(host='127.0.0.1',port=8765,debug=False,use_reloader=False,threaded=True)

if __name__=='__main__': main()
