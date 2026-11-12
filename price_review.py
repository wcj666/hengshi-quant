"""Price/structure observations, not fixed risk stops or executable sell orders."""
from datetime import timedelta, time
from strategy_setup import completed_history, _now, _number, _calendar


def review_price(history,price,*,now=None,trading_dates=None):
    clock=_now(now);p=_number(price)
    result={'valid':False,'action':'wait','trigger':None,'reason':'完整日线待核验，不推算买卖点。','level':None}
    frame,error=completed_history(history,clock)
    days=_calendar(trading_dates,clock.date())
    completed=[d for d in days or [] if d<clock.date() or (d==clock.date() and clock.time()>=time(15,5))]
    if error or frame is None or len(frame)<60 or not completed or frame.date.iloc[-1]!=completed[-1] or not p:return result
    close=frame.close;vol=frame.volume
    ma20=float(close.tail(20).mean());old20=float(close.iloc[-25:-5].mean())
    support=float(frame.low.tail(20).min());resistance=float(frame.high.tail(20).max())
    previous5=float(vol.iloc[-6:-1].mean());ratio=float(vol.iloc[-1])/previous5 if previous5>0 else None
    result.update(valid=True,asof=str(frame.date.iloc[-1]),support=support,resistance=resistance,ma20=ma20,volume_ratio5=ratio,
                  action='hold',reason='尚未触发结构走弱或压力位观察；继续复核量价，不按持有天数卖出。')
    if p<support:
        result.update(action='review',trigger='structure_support',level=support,reason='当前价跌破最近20个完整交易日低点，复核结构失效与减仓条件；这是结构观察，不是已设定的固定止损或已成交卖出。')
    elif p<ma20 and float(close.iloc[-1])<ma20 and ma20<old20 and ratio is not None and ratio>=1.2:
        result.update(action='review',trigger='structure_weakness',level=ma20,reason='完整日线放量收在MA20下方且MA20下降，当前价仍弱于MA20；复核持仓风险，不因单一日期卖出。')
    elif p>=resistance:
        result.update(action='review',trigger='resistance_test',level=resistance,reason='当前价触及最近20个完整交易日高点，观察突破或受阻后的量价表现；触及压力本身不代表应该卖出。')
    result['note']='结构观察阈值尚未回测验证；不替代固定风险计划，不核定卖出数量，成交与T+1须在券商确认。'
    return result
