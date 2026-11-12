"""Fetch real completed historical OHLC for stocks, ETFs, and convertible bonds."""
from datetime import date, datetime, timedelta
import math
import re
from zoneinfo import ZoneInfo
import pandas as pd


_TX_URL = 'https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get'
_CST = ZoneInfo('Asia/Shanghai')


def _tx_symbol(code, asset_type):
    code = str(code)
    if not re.fullmatch(r'\d{6}', code):
        raise ValueError('历史行情代码必须为6位数字')
    if asset_type == 'index':
        if code != '000300':
            raise ValueError('仅支持已核验的沪深300指数')
        return 'sh000300'
    if asset_type not in ('stock', 'etf'):
        raise ValueError('腾讯日线仅用于已核验类型')
    if asset_type == 'etf':
        if code.startswith(('5', '6')):return 'sh'+code
        if code.startswith(('1', '0', '3')):return 'sz'+code
        raise ValueError('ETF市场代码待核验')
    if code.startswith('6'):return 'sh'+code
    if code.startswith(('0', '3')):return 'sz'+code
    if code.startswith(('4', '8', '9')):return 'bj'+code
    raise ValueError('股票市场代码待核验')


def _tx_frame(payload, symbol, *, adjust, begin, end, today):
    """Parse one provider response without mixing adjusted and raw candles."""
    if not isinstance(payload, dict) or isinstance(payload.get('code'),bool) or payload.get('code') != 0:
        raise ValueError('腾讯历史行情返回失败状态')
    data = payload.get('data')
    if not isinstance(data, dict) or not isinstance(data.get(symbol), dict):
        raise ValueError('腾讯历史行情证券身份不匹配')
    key = 'qfqday' if adjust == 'qfq' else 'day'
    rows = data[symbol].get(key)
    # In particular, raw `day` is NEVER accepted for a stock qfq request.
    if not isinstance(rows, list) or not rows or len(rows) > 641:
        raise ValueError('腾讯历史行情为空、过量或缺少所需复权字段')
    records = []
    amount_complete = True
    seen = set()
    for row in rows:
        if not isinstance(row, list) or len(row) < 6 or not isinstance(row[0], str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',row[0]):
            raise ValueError('腾讯真实OHLCV字段不完整')
        day = date.fromisoformat(row[0])
        if day > today or day in seen:
            raise ValueError('腾讯历史日线包含未来或重复日期')
        seen.add(day)
        values = []
        for value in row[1:6]:
            if isinstance(value, bool):raise ValueError('腾讯日线数值无效')
            number = float(value)
            if not math.isfinite(number):raise ValueError('腾讯日线数值非有限')
            values.append(number)
        opened, close, high, low, volume = values
        if (min(opened,close,high,low) <= 0 or volume < 0 or
                high < max(opened,close,low) or low > min(opened,close,high)):
            raise ValueError('腾讯日线价格或成交量关系无效')
        # Same newfqkline schema/units as AKShare stock_hist_tx: column 5 is
        # shares for STAR 688/689, lots for other securities and indexes.
        multiplier = 1 if symbol.startswith(('sh688','sh689')) else 100
        if not math.isfinite(volume*multiplier):raise ValueError('腾讯成交量单位换算溢出')
        record = {'date':pd.Timestamp(day),'open':opened,'close':close,
                  'high':high,'low':low,'volume':volume*multiplier}
        if len(row) > 8 and row[8] not in (None, ''):
            amount = float(row[8])
            if isinstance(row[8],bool) or not math.isfinite(amount) or amount < 0:
                raise ValueError('腾讯成交额字段无效')
            if not math.isfinite(amount*10000):raise ValueError('腾讯成交额单位换算溢出')
            record['amount'] = amount*10000  # provider's unit is RMB 10,000
        else:
            amount_complete = False
        if begin <= day <= end:records.append(record)
    if not records:raise ValueError('腾讯历史行情在请求日期内为空')
    frame = pd.DataFrame(records).sort_values('date').reset_index(drop=True)
    # No price*volume fabrication: missing amount keeps liquidity entry checks
    # blocked while real OHLCV still supports historical structure research.
    if not amount_complete and 'amount' in frame:frame = frame.drop(columns=['amount'])
    frame.attrs.update(source='腾讯公开历史日K',adjust=adjust,
                       volume_unit='shares',amount_unit='CNY',max_source_rows=641)
    return frame


def tencent_history(code, asset_type, begin, end=None):
    """One bounded HTTPS call; Tencent caps a response at ~640 daily bars.

    It replaces, rather than appends to, another provider's adjusted history.
    Completion/calendar checks remain with callers; today's intraday candle is
    not automatically certified complete merely because it exists here.
    """
    import requests
    today = datetime.now(_CST).date()
    start = pd.to_datetime(begin,format='%Y%m%d',errors='raise').date()
    finish = pd.to_datetime(end,format='%Y%m%d',errors='raise').date() if end else today
    finish = min(finish,today)
    if start > finish:raise ValueError('历史日线开始日期不能晚于结束日期')
    symbol = _tx_symbol(code,asset_type)
    adjust = 'qfq' if asset_type == 'stock' else ''
    # Tencent's explicit end boundary is exclusive and can omit the current
    # closing candle. An empty endpoint gets its latest series; filter locally.
    boundary = '' if finish == today else (finish+timedelta(days=1)).isoformat()
    response = requests.get(_TX_URL,params={'param':f'{symbol},day,,{boundary},640,{adjust}'},timeout=(3,5))
    response.raise_for_status()
    if len(response.content) > 2_000_000:raise ValueError('腾讯历史行情响应超过限额')
    return _tx_frame(response.json(),symbol,adjust=adjust,begin=start,end=finish,today=today)


def _norm(df):
    if df is None or df.empty:raise ValueError('上游历史行情为空')
    if 'date' not in df and df.index.name=='date':df=df.reset_index()
    df=df.rename(columns={'日期':'date','开盘':'open','收盘':'close','最高':'high','最低':'low','成交量':'volume','成交额':'amount'}).copy()
    need={'date','open','close','high','low','volume'}
    if not need.issubset(df.columns):raise ValueError('数据源缺少真实OHLCV字段，暂停分析')
    df['date']=pd.to_datetime(df['date'],errors='coerce')
    for x in ('open','close','high','low','volume'):df[x]=pd.to_numeric(df[x],errors='coerce')
    if 'amount' in df:df['amount']=pd.to_numeric(df['amount'],errors='coerce')
    df=df.dropna(subset=list(need)).sort_values('date')
    if df['date'].duplicated().any():raise ValueError('数据源包含重复交易日期，暂停分析')
    return df.reset_index(drop=True)


def history_by_asset(code:str,asset_type:str,begin:str,end:str|None=None):
    end=end or datetime.now(_CST).strftime('%Y%m%d')
    if asset_type in ('stock','etf'):
        try:return tencent_history(code,asset_type,begin,end)
        except Exception as primary_error:
            try:
                if asset_type=='stock':
                    from app import stock_history
                    return _norm(stock_history(code,begin))
                import akshare as ak
                return _norm(ak.fund_etf_hist_em(symbol=code,period='daily',start_date=begin,end_date=end,adjust=''))
            except Exception as fallback_error:
                raise RuntimeError(f'腾讯历史日线失败：{type(primary_error).__name__}；备用源失败：{type(fallback_error).__name__}') from fallback_error
    if asset_type=='convertible':
        import akshare as ak
        prefix='sh' if code.startswith('11') else 'sz'
        df=ak.bond_zh_hs_cov_daily(symbol=prefix+code)
        df=_norm(df)
        return df[(df['date']>=pd.to_datetime(begin))&(df['date']<=pd.to_datetime(end))].copy()
    raise ValueError('未知证券类型，不能请求股票历史行情作为替代')


def benchmark_history(begin, end=None):
    """Real unadjusted CSI300 daily candles, never a stock with the same code."""
    try:return tencent_history('000300','index',begin,end)
    except Exception as primary_error:
        try:
            import akshare as ak
            return _norm(ak.index_zh_a_hist(symbol='000300',period='daily',start_date=begin,
                                          end_date=end or datetime.now(_CST).strftime('%Y%m%d')))
        except Exception as fallback_error:
            raise RuntimeError(f'腾讯沪深300日线失败：{type(primary_error).__name__}；备用源失败：{type(fallback_error).__name__}') from fallback_error
