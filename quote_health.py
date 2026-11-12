"""A-share quote provenance and anomaly checks.

Fetch time is NOT exchange quote time. A missing provider timestamp remains unverified.
Unchanged prices alone are a suspicion, not definitive evidence of a frozen feed.
"""
from __future__ import annotations
from datetime import datetime, timezone
import math


def numeric(x):
    try:
        f=float(x)
        return f if math.isfinite(f) and f > 0 else None
    except (TypeError,ValueError):return None


def parse_provider_time(value):
    """Only full timezone-aware datetimes can establish quote freshness."""
    if not isinstance(value,str) or len(value)<19:return None
    try:
        when=datetime.fromisoformat(value.replace('Z','+00:00'))
        return when.astimezone(timezone.utc) if when.tzinfo else None
    except ValueError:return None


def assess(cfg, feed, previous=None, now=None):
    now=now or datetime.now(timezone.utc)
    prev_by=(previous or {}).get('quote_health',{}).get('by_code',{})
    by={}; missing=[];invalid=[];suspected=[]; stale_source=[];unverified_source=[];verified_count=0
    positions=[x for x in cfg.get('positions',[]) if int(x.get('quantity',0))>0]
    for p in positions:
        code=str(p['code']).zfill(6)
        quote=feed.get((p['type'],code))
        price=numeric(quote[0]) if quote else None
        prior=prev_by.get(code,{})
        if price is None:
            missing.append(code)
            by[code]={'status':'missing','price':None,'consecutive_same':0,'source_timestamp':None,'source_verified':False}
            continue
        when=parse_provider_time(quote[3]) if quote and len(quote)>3 else None
        verified=bool(when and 0 <= (now-when).total_seconds() <= 90)
        if when and not verified:stale_source.append(code)
        if verified:verified_count+=1
        elif when is None:unverified_source.append(code)
        last_price=numeric(prior.get('price'))
        consecutive=(int(prior.get('consecutive_same',0))+1) if last_price==price else 0
        last_changed=(prior.get('last_changed_at') if last_price==price else now.isoformat()) or now.isoformat()
        try:
            first=datetime.fromisoformat(last_changed)
            unchanged_seconds=(now-first.astimezone(timezone.utc)).total_seconds() if first.tzinfo else 0
        except ValueError:unchanged_seconds=0
        # A long flat print without a provider clock is suspicious, not proof of stale quotes.
        suspect=bool(not verified and consecutive>=12 and unchanged_seconds>=120)
        if suspect:suspected.append(code)
        if last_price:
            ratio=abs(price/last_price-1)
            max_jump={'stock':.32,'etf':.32,'convertible':.85}.get(p['type'],.32)
            if ratio>max_jump:invalid.append(code)
        by[code]={'price':price,'status':('invalid_jump' if code in invalid else 'stale_source' if code in stale_source else 'suspected_freeze' if suspect else 'ok'),
                  'consecutive_same':consecutive,'last_changed_at':last_changed,
                  'source_timestamp':when.isoformat() if when else None,'source_verified':verified,
                  'unchanged_seconds':round(max(0,unchanged_seconds))}
    # Allow indicative risk display with unknown provider time, but withhold
    # actionable-looking NEW trade alerts when a defect or freeze is detected.
    complete=not missing and not invalid and not stale_source
    blocked=bool(missing or invalid or suspected or stale_source)
    trade_blocked=bool(blocked or unverified_source)
    return {'by_code':by,'missing':missing,'invalid_jump':invalid,'suspected_freeze':suspected,
            'stale_source':stale_source,'unverified_source':unverified_source,
            'all_held_usable':complete,'block_new_signals':blocked,
            'block_trade_signals':trade_blocked,
            'provider_timestamps_verified':bool(positions) and verified_count==len(positions),
            'verified_count':verified_count,'held_count':len(positions),
            'note':'采集完成时间≠交易所报价时间；源时间戳缺失时仅显示估算，不触发新的买卖/止盈止损提醒。价格长期不变是疑似停滞，不证明停牌或错误。'}
