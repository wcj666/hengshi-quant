"""Bounded Tencent minute observations for T research. No order or stale cache.

Tencent rows carry *cumulative* volume (hands) and turnover (RMB). Neither is
converted to per-minute activity here. The quote clock is the provider's field
30, never the local fetch time; downstream checks may still reject observations.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone, timedelta
import math
import re
import threading
import time
from zoneinfo import ZoneInfo

import requests
from portfolio_quotes import _tencent_symbol

CN = ZoneInfo('Asia/Shanghai')
SOURCE = '腾讯公开分时 appstock minute（累计成交量/额，非交易所逐笔实时）'
_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix='t-minute')
_FLIGHT_LOCK = threading.Lock()
_FLIGHTS = {}


def _now(value=None):
    value = value or datetime.now(CN)
    return value.replace(tzinfo=CN) if value.tzinfo is None else value.astimezone(CN)


def _invalid(error, *, source_timestamp=None, asof=None):
    return {'valid': False, 'rows': [], 'source_timestamp': source_timestamp,
            'asof': asof, 'error': error, 'source': SOURCE}


def _number(value, *, positive=False):
    try:n=float(value)
    except (TypeError, ValueError):raise ValueError('分时数值缺失或无效')
    if not math.isfinite(n) or n < 0 or positive and n <= 0:
        raise ValueError('分时数值为负、非有限值或价格无效')
    return n


def parse_t_minutes(payload, code, now=None):
    """Validate one raw response. Current minute is excluded, never extrapolated."""
    clock = _now(now);source_timestamp=None;asof=None
    try:
        symbol=_tencent_symbol(code);code=str(code).zfill(6)
        if not isinstance(payload,dict) or payload.get('code') not in (0,'0'):
            raise ValueError('腾讯分时接口未返回成功状态')
        section=payload.get('data',{}).get(symbol)
        if not isinstance(section,dict):raise ValueError('腾讯分时未返回匹配证券代码')
        data=section.get('data');qt=section.get('qt')
        if not isinstance(data,dict) or not isinstance(qt,dict):raise ValueError('腾讯分时结构或源行情缺失')
        quote=qt.get(symbol)
        if not isinstance(quote,list) or len(quote)<=30 or str(quote[2])!=code:
            raise ValueError('腾讯分时行情证券代码不匹配或源时间缺失')
        raw_stamp=str(quote[30])
        if not re.fullmatch(r'\d{14}',raw_stamp):raise ValueError('腾讯分时源时间不是完整日期时间')
        source=datetime.strptime(raw_stamp,'%Y%m%d%H%M%S').replace(tzinfo=CN)
        source_timestamp=source.isoformat()
        raw_date=str(data.get('date',''))
        if not re.fullmatch(r'\d{8}',raw_date):raise ValueError('腾讯分时日期缺失或无效')
        day=datetime.strptime(raw_date,'%Y%m%d').date();asof=day.isoformat()
        if day!=clock.date() or source.date()!=day:raise ValueError('腾讯分时并非当日数据或源报价日期不一致')
        source_age=(clock-source).total_seconds()
        if not 0<=source_age<=90:raise ValueError('腾讯分时源时间超90秒或来自未来')
        raw_rows=data.get('data')
        if not isinstance(raw_rows,list) or not raw_rows or len(raw_rows)>300:
            raise ValueError('腾讯分时行缺失或数量异常')
        completed=[];previous=None;last_volume=None;last_amount=None
        minute=clock.replace(second=0,microsecond=0)
        for raw in raw_rows:
            if not isinstance(raw,str):raise ValueError('腾讯分时行格式无效')
            fields=raw.split()
            if len(fields)!=4 or not re.fullmatch(r'\d{4}',fields[0]):raise ValueError('腾讯分时缺少时间、价格、累计量或额')
            stamp=datetime.strptime(raw_date+fields[0],'%Y%m%d%H%M').replace(tzinfo=CN)
            hm=stamp.hour*60+stamp.minute
            if not (570<=hm<=690 or 780<=hm<=900):raise ValueError('腾讯分时包含非连续竞价时段数据')
            # Tencent labels the in-progress row with the next minute's end
            # time. Discard both the current and next-labelled row; neither
            # can establish a completed-bar turn. Further future rows fail.
            if stamp>minute+timedelta(minutes=1):raise ValueError('腾讯分时包含超出未完成分钟范围的未来数据')
            if previous is not None and stamp<=previous:raise ValueError('腾讯分时时间重复或逆序')
            if stamp>=minute:
                # Partial end-labelled rows may also carry provisional volume;
                # neither their prices nor their counters enter completed data.
                previous=stamp
                continue
            price=_number(fields[1],positive=True);volume=_number(fields[2]);amount=_number(fields[3])
            if (last_volume is not None and volume<last_volume or
                    last_amount is not None and amount<last_amount):
                raise ValueError('腾讯分时累计成交量或额回退')
            if volume != int(volume):raise ValueError('腾讯分时累计成交量单位异常')
            if (volume==0)!=(amount==0):raise ValueError('腾讯分时累计量额不一致')
            if stamp<minute:
                completed.append({'timestamp':stamp.isoformat(),'price':price,
                                  'cum_volume':volume,'cum_amount':amount})
            previous=stamp;last_volume=volume;last_amount=amount
        if len(completed)<30:raise ValueError('完整分时不足30根，不生成做T观察')
        last=datetime.fromisoformat(completed[-1]['timestamp'])
        if not 0<(clock-last).total_seconds()<=150:raise ValueError('最近完整分时超过150秒或无效')
        if not completed[-1]['cum_volume'] or not completed[-1]['cum_amount']:
            raise ValueError('完整分时无有效成交量额')
        return {'valid':True,'rows':completed,'source_timestamp':source_timestamp,'asof':asof,
                'error':None,'source':SOURCE,'volume_unit':'手（100份/股）','amount_unit':'人民币元'}
    except (KeyError,TypeError,ValueError,AttributeError,OverflowError) as exc:
        return _invalid(str(exc),source_timestamp=source_timestamp,asof=asof)


def _fetch_payload(code):
    symbol=_tencent_symbol(code)
    response=requests.get('https://web.ifzq.gtimg.cn/appstock/app/minute/query',
                          params={'code':symbol},headers={'Referer':'https://gu.qq.com/','User-Agent':'Mozilla/5.0'},
                          timeout=(2,3))
    response.raise_for_status()
    return response.json()


def _start(code):
    with _FLIGHT_LOCK:
        prior=_FLIGHTS.get(code)
        if prior is not None and not prior.done():return None
        # A late result is discarded. A new cycle must make a new request.
        if prior is not None:_FLIGHTS.pop(code,None)
        future=_POOL.submit(_fetch_payload,code);_FLIGHTS[code]=future
        return future


def _release(code,future):
    with _FLIGHT_LOCK:
        if _FLIGHTS.get(code) is future and future.done():_FLIGHTS.pop(code,None)


def fetch_t_minutes(cfg,now=None,*,deadline_seconds=8.0):
    """Fetch held stock/ETF minute series in <=8s, with one flight per code.

    Missing and timed-out responses remain invalid this cycle. There is no
    replay of an earlier or timed-out response and no fabricated source clock.
    Callers should omit ``now`` for live requests so post-fetch freshness uses
    the current clock. Supplying ``now`` is for deterministic offline checks.
    """
    if not 0.01<=deadline_seconds<=8:raise ValueError('分时等待期限必须在0.01至8秒内')
    started=time.monotonic();codes=set();result={};pending={}
    for position in cfg.get('positions',[]):
        try:
            if position.get('type') not in ('stock','etf') or int(position.get('quantity',0))<=0:continue
            code=str(position.get('code','')).zfill(6);_tencent_symbol(code);codes.add(code)
        except (TypeError,ValueError):
            code=str(position.get('code',''));result[code]=_invalid('持仓代码或数量无效')
    for code in sorted(codes):
        future=_start(code)
        if future is None:result[code]=_invalid('该证券上一轮分时请求仍未结束，本轮不重复请求')
        else:pending[code]=future
    if not pending:return result
    done,_=wait(list(pending.values()),timeout=max(0,deadline_seconds-(time.monotonic()-started)))
    for code,future in pending.items():
        if future not in done:
            future.cancel() # Cancel queued work; a running request remains single-flight.
            result[code]=_invalid(f'腾讯分时超过{deadline_seconds:g}秒，本轮不采用迟到数据')
        else:
            try:result[code]=parse_t_minutes(future.result(),code,now)
            except Exception as exc:result[code]=_invalid('腾讯分时请求失败：'+type(exc).__name__)
        _release(code,future)
    return result
