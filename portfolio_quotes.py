"""Tencent public batch quotes for manually maintained positions/watchlist.

10s refers to the *target start interval*, not data feed freshness.
Do not reuse old prices as fresh, nor invent provider timestamps.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, wait
import math
import re
import threading
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


QUOTE_SOURCE = '腾讯公开行情 qt.gtimg.cn（提供方报价时间，非交易所逐笔实时）'


def _price(value):
    try:
        n=float(value)
        return n if math.isfinite(n) and n>0 else None
    except (ValueError, TypeError):
        return None


def _key(code):
    return str(code).zfill(6)


def _tencent_symbol(code):
    code = _key(code)
    if not re.fullmatch(r'\d{6}', code):
        raise ValueError('腾讯行情需要6位证券代码')
    if code.startswith(('5', '6', '9', '11')):
        return 'sh' + code
    if code.startswith(('0', '1', '2', '3')):
        return 'sz' + code
    raise ValueError('暂不支持该证券交易所代码')


def _parse_tencent_quotes(text, targets):
    """Parse the provider's GB18030-decoded response without executing its JS.

    Fields 1/2/3/4/30 are name/code/last/previous close/provider datetime.
    A missing or malformed provider clock stays None for downstream gating.
    """
    expected = {}
    for kind, code in targets:
        expected.setdefault(_tencent_symbol(code), []).append((kind, _key(code)))
    rows = {}
    for symbol, body in re.findall(r'v_((?:sh|sz)\d{6})\s*=\s*"([^"\r\n]*)"\s*;', text):
        if symbol in expected:
            rows.setdefault(symbol, []).append(body.split('~'))
    feed, errors = {}, []
    for symbol, keys in sorted(expected.items()):
        matches = rows.get(symbol, [])
        if len(matches) != 1:
            errors.append(f'腾讯行情 {symbol} ' + ('未返回报价' if not matches else '返回重复报价，本轮不采用'))
            continue
        fields = matches[0]
        if len(fields) < 5 or fields[2] != symbol[2:] or _price(fields[3]) is None:
            errors.append(f'腾讯行情 {symbol} 代码不匹配或最新价无效')
            continue
        stamp = None
        if len(fields) > 30 and re.fullmatch(r'\d{14}', fields[30]):
            try:
                stamp = datetime.strptime(fields[30], '%Y%m%d%H%M%S').replace(
                    tzinfo=ZoneInfo('Asia/Shanghai')).astimezone(timezone.utc).isoformat()
            except ValueError:
                pass
        # Keep provider-reported opening price separate from last price. V1
        # rejects a next-day plan that opened beyond its cap even if it retraces.
        limit=_price(fields[47]) if len(fields)>47 else None
        lower=_price(fields[48]) if len(fields)>48 else None
        metadata={'open':_price(fields[5]) if len(fields)>5 else None,'upper_limit':limit,'lower_limit':lower,'limit_status_verified':limit is not None and lower is not None,
                  'at_limit_down':bool(_price(fields[3])<=lower+0.000001) if lower is not None else None,
                  'at_limit_up':bool(_price(fields[3])>=limit-0.000001) if limit is not None else None}
        # Snapshot order book for conservative PAPER fills only. Missing sizes
        # remain missing; neither last price nor daily volume substitutes for it.
        for name,index in (('bid1',9),('ask1',19)):
            metadata[name]=_price(fields[index]) if len(fields)>index else None
            lots=_price(fields[index+1]) if len(fields)>index+1 else None
            metadata[name+'_shares']=int(lots*100) if lots is not None else None
        quote = (_price(fields[3]), fields[1], _price(fields[4]), stamp, metadata)
        for key in keys:
            feed[key] = quote
    return feed, errors


def _tencent_batch(_unused, targets):
    symbols = sorted({_tencent_symbol(code) for _, code in targets})
    # Retry one transient transport failure inside the observer's bounded wait;
    # the endpoint-wide flight prevents another poll overlapping a late retry.
    for attempt in range(2):
        try:
            response = requests.get('https://qt.gtimg.cn/', params={'q': ','.join(symbols)},
                headers={'Referer': 'https://gu.qq.com/', 'User-Agent': 'Mozilla/5.0'}, timeout=(3, 4))
            response.raise_for_status()
            break
        except requests.RequestException:
            if attempt:
                raise
    # Tencent's Chinese names use GBK/GB18030; requests' guessed Latin-1 corrupts them.
    return _parse_tencent_quotes(response.content.decode('gb18030'), targets)


def snapshot_selected_tencent(cfg, *, include_watch=True, deadline_seconds=7.5):
    """Fetch all configured targets in one bounded, single-flight batch.

    Missing rows and timed-out responses never fall back to a previous cycle.
    Timestamp freshness is checked by quote_health, using the provider clock.
    """
    if not 0.1 <= deadline_seconds <= 60:
        raise ValueError('查询截止时间必须在0.1—60秒之间')
    positions = {(p['type'], _key(p['code'])) for p in cfg.get('positions', [])
                 if int(p.get('quantity', 0)) > 0}
    watch = {(p.get('type','stock'), _key(p['code'])) for p in cfg.get('watchlist', [])} if include_watch else set()
    # A held ETF/convertible may also appear in watchlist; keep its real asset type.
    held_codes = {code for _, code in positions}
    targets = positions | {(kind, code) for kind, code in watch if code not in held_codes}
    if not targets:
        return {}, []
    valid_targets, errors = set(), []
    for kind, code in sorted(targets):
        try:
            if kind not in ('stock', 'etf', 'convertible'):
                raise ValueError('不支持的证券类型')
            _tencent_symbol(code)
            valid_targets.add((kind, code))
        except ValueError as exc:
            errors.append(f'腾讯行情 {kind} {code}: {exc}')
    if not valid_targets:
        return {}, errors
    # A single endpoint-wide flight also prevents the web analysis endpoint from
    # starting duplicate requests while a background batch is still outstanding.
    key = ('tencent',)
    future, reason = _task_ready(key, _tencent_batch, None, valid_targets)
    if future is None:
        return {}, errors + [f'腾讯批量行情：{reason}']
    finished, _ = wait([future], timeout=deadline_seconds)
    if not finished:
        return {}, errors + [f'腾讯批量行情超过{deadline_seconds:g}秒，本轮报价作缺失处理']
    _release_finished(key, future)
    try:
        feed, failures = future.result()
        return feed, errors + failures
    except Exception as exc:
        return {}, errors + [f'腾讯批量行情失败：{type(exc).__name__}: {exc}']


def tencent_full_snapshot(*, deadline_seconds=45.0):
    """Fetch a broad A-share snapshot from Tencent for full-market fallback.

    Tencent does not publish industry labels or a reliable volume-ratio field
    in this endpoint. Those fields stay empty so the scanner cannot invent them.
    """
    import pandas as pd
    if not 5 <= deadline_seconds <= 180:
        raise ValueError('全市场腾讯查询截止时间必须在5—180秒之间')
    codes=[*(f'{x:06d}' for x in range(1,4000)),
           *(f'{x:06d}' for x in range(300000,301000)),
           *(f'{x:06d}' for x in range(600000,606000)),
           *(f'{x:06d}' for x in range(688000,689000))]
    symbols=[_tencent_symbol(c) for c in codes]
    chunks=[symbols[i:i+400] for i in range(0,len(symbols),400)]
    started=datetime.now(timezone.utc)

    def fetch(chunk):
        response=requests.get('https://qt.gtimg.cn/',params={'q':','.join(chunk)},
            headers={'Referer':'https://gu.qq.com/','User-Agent':'Mozilla/5.0'},timeout=(3,8))
        response.raise_for_status()
        return response.content.decode('gb18030','replace')

    rows=[];errors=[]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures=[pool.submit(fetch,chunk) for chunk in chunks]
        finished,_=wait(futures,timeout=deadline_seconds)
        for future in futures:
            if future not in finished:
                errors.append('腾讯全市场批次超时');continue
            try:
                text=future.result()
                for symbol,body in re.findall(r'v_((?:sh|sz)\d{6})\s*=\s*"([^"\r\n]*)"\s*;',text):
                    fields=body.split('~')
                    if len(fields)<39 or not re.fullmatch(r'\d{6}',fields[2]):continue
                    current=_price(fields[3]);previous=_price(fields[4])
                    if current is None or previous is None:continue
                    amount=None
                    if len(fields)>35:
                        pieces=fields[35].split('/')
                        if len(pieces)>=3:amount=_price(pieces[2])
                    market_cap=_price(fields[72]) if len(fields)>72 else None
                    turnover=_price(fields[38]) if len(fields)>38 else None
                    rows.append({'code':fields[2],'name':fields[1],'price':current,
                        'pct':round((current/previous-1)*100,4),'amount':amount or 0.0,
                        'turnover':turnover if turnover is not None else 0.0,
                        'volume_ratio':None,'market_cap':market_cap or 0.0})
            except Exception as exc:errors.append(f'腾讯批次失败：{type(exc).__name__}: {exc}')
    if not rows:raise RuntimeError('腾讯全市场行情没有返回有效股票')
    frame=pd.DataFrame(rows).drop_duplicates('code').reset_index(drop=True)
    elapsed=(datetime.now(timezone.utc)-started).total_seconds()
    return frame, f'腾讯公开行情 qt.gtimg.cn，全市场批次耗时{elapsed:.1f}秒（行业和量比字段未提供）', errors


def _stock_quote(ak, code):
    if ak is None:
        return _security_quote(code)
    # Injectable AKShare-compatible provider for offline fixtures.
    frame=ak.stock_bid_ask_em(symbol=code)
    if frame is None or frame.empty or not {'item','value'}.issubset(frame.columns):
        raise ValueError('个股盘口缺少 item/value 报价字段')
    vals={str(a).strip():b for a,b in zip(frame['item'],frame['value'])}
    current=next((_price(vals[k]) for k in ('最新','最新价','现价') if _price(vals.get(k)) is not None),None)
    previous=next((_price(vals[k]) for k in ('昨收','昨收价','昨收盘') if _price(vals.get(k)) is not None),None)
    if previous is None and current is not None:
        # Bid/ask may expose only absolute price change, not previous close.
        for k in ('涨跌','涨跌额'):
            try:
                move=float(vals[k]); cand=_price(current-move)
                if cand is not None:previous=cand;break
            except (KeyError, TypeError, ValueError):continue
    if current is None:raise ValueError('个股盘口未返回有效最新价')
    # A field containing a full, offset-aware datetime may be used as a provider clock.
    # Time-of-day alone is NOT enough to verify quote freshness.
    raw_ts=next((str(vals[k]).strip() for k in ('报价时间','更新时间','时间') if k in vals and vals[k] is not None),None)
    return (current,'',previous,raw_ts)


def _security_quote(code):
    # The raw provider response includes f86 (provider update epoch). AKShare's
    # bid/ask dataframe drops it. Never replace a missing f86 with fetch time.
    market = '1' if code.startswith(('5','6')) else '0'
    response = requests.get('https://push2.eastmoney.com/api/qt/stock/get',
        params={'secid':f'{market}.{code}', 'fields':'f43,f57,f58,f60,f86',
                'fltt':'2', 'invt':'2'}, timeout=(2, 4))
    response.raise_for_status()
    data = response.json().get('data') or {}
    current = _price(data.get('f43'))
    if str(data.get('f57')) != code or current is None:
        raise ValueError('行情接口未返回匹配代码的有效最新价')
    stamp=None
    try:
        epoch=float(data['f86'])
        if math.isfinite(epoch) and epoch>0:
            stamp=datetime.fromtimestamp(epoch,timezone.utc).isoformat()
    except (KeyError,TypeError,ValueError,OverflowError,OSError):pass
    return current, str(data.get('f58') or ''), _price(data.get('f60')), stamp


def _etf_quote(ak, code):
    return _security_quote(code)


def _spot_frame(ak, kind):
    if ak is None:
        import akshare as ak
    if kind=='etf':
        frame=ak.fund_etf_spot_em()
        code_field,name_field,price_field='代码','名称','最新价'
    else:
        frame=ak.bond_zh_hs_cov_spot()
        code_field,name_field,price_field='code','name','trade'
    if frame is None or frame.empty or any(x not in frame for x in (code_field,price_field)):
        raise ValueError(kind+' 报价字段缺失或返回空结果')
    quotes={}
    for _,row in frame.iterrows():
        code=_key(row[code_field]); current=_price(row[price_field]); previous=_price(row.get('昨收'))
        if current is None:continue
        if kind=='convertible':
            try:
                move=float(row.get('pricechange'))
                if math.isfinite(move): previous=_price(current-move)
            except (ValueError,TypeError):pass
        quotes[code]=(current,str(row.get(name_field,'')),previous)
    return quotes


# A bounded worker pool avoids one hung provider call spawning unlimited threads.
# Timed-out results are never reused as a new quote on a later polling cycle.
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix='hengshi-quote')
_INFLIGHT = {}
_TASK_LOCK = threading.Lock()


def _task_ready(key, fn, ak, arg):
    """Submit only when no previous upstream call for the same key is running."""
    with _TASK_LOCK:
        old=_INFLIGHT.get(key)
        if old is not None:
            if not old.done():
                return None, '前一轮接口请求尚未结束，本轮跳过重试（不沿用旧报价）'
            # Discard prior-cycle output even if it finally succeeded.
            _INFLIGHT.pop(key,None)
        future=_EXECUTOR.submit(fn,ak,arg)
        _INFLIGHT[key]=future
        return future,None


def _release_finished(key,future):
    with _TASK_LOCK:
        if _INFLIGHT.get(key) is future:
            _INFLIGHT.pop(key,None)


def snapshot_selected_akshare(cfg, *,ak=None, include_watch=True,deadline_seconds=7.5):
    """Compatibility entry point: live requests use Tencent batch quotes.

    An explicitly injected AKShare-compatible provider is kept for offline adapter
    fixtures. No live AKShare/Eastmoney fallback is made by the default path.
    """
    if ak is None:
        return snapshot_selected_tencent(cfg, include_watch=include_watch,
                                         deadline_seconds=deadline_seconds)
    if not 0.1<=deadline_seconds<=60:
        raise ValueError('查询截止时间必须在0.1—60秒之间')
    positions={(p['type'],_key(p['code'])) for p in cfg.get('positions',[]) if int(p.get('quantity',0))>0}
    watch={(p.get('type','stock'),_key(p['code'])) for p in cfg.get('watchlist',[])} if include_watch else set()
    targets=positions|watch
    if not targets:return {},[]
    held_stocks=sorted(code for kind,code in positions if kind=='stock')
    watched_stocks=sorted(code for kind,code in targets if kind=='stock' and ('stock',code) not in positions)
    etfs=sorted(code for kind,code in targets if kind=='etf')
    kinds=sorted({kind for kind,_ in targets if kind=='convertible'})
    jobs=[('stock',code,_stock_quote,code) for code in held_stocks]
    jobs += [('etf',code,_etf_quote,code) for code in etfs]
    jobs += [(kind,'all',_spot_frame,kind) for kind in kinds]
    jobs += [('stock',code,_stock_quote,code) for code in watched_stocks]
    futures={};errors=[]
    # The provider identity is part of the task key so unit-test fake providers
    # cannot pick up another provider's incomplete task.
    for kind,code,func,arg in jobs:
        key=(id(ak),kind,code)
        f,reason=_task_ready(key,func,ak,arg)
        if f is None:
            errors.append(f'{kind} {code}: {reason}')
        else:
            futures[f]=(key,kind,code)
    finished,unfinished=wait(futures,timeout=deadline_seconds)
    feed={}
    for f in finished:
        key,kind,code=futures[f]
        _release_finished(key,f)
        try:
            result=f.result()
            if code!='all':
                if (kind,code) in targets:feed[(kind,code)]=result
            else:
                for asset,quote in result.items():
                    if (kind,asset) in targets:feed[(kind,asset)]=quote
        except Exception as exc:
            errors.append(f'{kind} {code} 行情失败：{type(exc).__name__}: {exc}')
    for f in unfinished:
        key,kind,code=futures[f]
        # Keep the future in _INFLIGHT until it exits. Never show it as fresh.
        errors.append(f'{kind} {code} 超过{deadline_seconds:g}秒仍无结果，本轮报价作缺失处理')
    return feed,errors
