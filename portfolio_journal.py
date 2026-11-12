"""Explicit manual trade history and forward-only estimated equity observations."""
from __future__ import annotations
from datetime import datetime,timezone
from pathlib import Path
from uuid import uuid4
import json, math, os


def read_lines(path,limit=500):
    path=Path(path)
    if not path.is_file():return []
    out=[]
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            val=json.loads(line)
            if isinstance(val,dict):out.append(val)
        except (ValueError,TypeError):pass
    return out[-limit:]


def log_manual_trade(path,data):
    required=('date','code','side','quantity','price','fee')
    if any(k not in data for k in required):raise ValueError('交易记录信息不完整')
    try:
        date=datetime.strptime(data['date'],'%Y-%m-%d').date()
        if date>datetime.now().date():raise ValueError('交易日期不能晚于今天')
        code=str(data['code']).strip()
        if len(code)!=6 or not code.isascii() or not code.isdigit():raise ValueError('证券代码需为6位数字')
        side=data['side']
        if side not in ('buy','sell'):raise ValueError('请选择买入或卖出')
        qty=int(data['quantity']); price=float(data['price']); fee=float(data['fee'])
        if float(data['quantity'])!=qty:raise ValueError('成交数量必须是整数')
        if qty<=0 or qty>100000000 or price<=0 or not math.isfinite(price) or not (0<=fee<=1e8) or not math.isfinite(fee):
            raise ValueError('数量、成交价或费用超出有效范围')
        note=str(data.get('note','')).strip()[:200]
    except (TypeError,KeyError,OverflowError) as e:raise ValueError('交易记录含有无效数据') from e
    entry={'id':uuid4().hex,'logged_at':datetime.now(timezone.utc).isoformat(),
           'date':str(date),'code':code,'side':side,'quantity':qty,'price':round(price,4),'fee':round(fee,2),
           'gross':round(qty*price,2),'net_cash_change':round(((-1 if side=='buy' else 1)*qty*price)-fee,2),'note':note}
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    # Append-only log; never silently edit the user's actual holdings or cash.
    with path.open('a',encoding='utf-8') as out:
        out.write(json.dumps(entry,ensure_ascii=False)+'\n');out.flush();os.fsync(out.fileno())
    return entry


def record_equity(path, *,at,config_hash,amount,source_status):
    """Record no more than one observation/5min. Only called on real live complete quotes.
    Observations are estimated snapshots, not verified broker NAV or audited P&L.
    """
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    try:
        moment=datetime.fromisoformat(at)
        if moment.tzinfo is None:raise ValueError('timestamp must have timezone')
        amount=float(amount)
        if not math.isfinite(amount) or amount<=0:raise ValueError('invalid equity')
    except (ValueError,TypeError):return False
    prior=read_lines(path,1)
    if prior:
        try:
            prev=datetime.fromisoformat(prior[-1]['at'])
            if (moment-prev).total_seconds()<300:return False
        except (ValueError,KeyError,TypeError):pass
    entry={'at':moment.isoformat(),'day_cn':moment.astimezone(__import__('zoneinfo').ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d'),
           'equity_estimate':round(amount,2),'config_hash':config_hash,'quality':source_status}
    with path.open('a',encoding='utf-8') as f:
        f.write(json.dumps(entry,ensure_ascii=False)+'\n')
    return True


def daily_curve(path):
    """Each date uses the last available snapshot. Never claims official close."""
    rows=read_lines(path,20000)
    days={}
    for x in rows:
        if x.get('day_cn') and isinstance(x.get('equity_estimate'),(int,float)):
            days[x['day_cn']]=x
    return [days[k] for k in sorted(days)][-120:]
