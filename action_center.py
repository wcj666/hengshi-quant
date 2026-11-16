"""Read-only action cards from verified live signals, never orders or forecasts."""
from datetime import datetime, time
from zoneinfo import ZoneInfo
from math import isfinite
from quote_health import parse_provider_time
from t_costs import estimate_leg

CN = ZoneInfo('Asia/Shanghai')


def exit_only(cfg):
    return (cfg.get('execution_preferences') or {}).get('mode') == 'exit_only'


def allowed_signals(cfg, signals):
    return [s for s in signals if not exit_only(cfg) or s.get('category') == 'reduce']


def number(value):
    return value if type(value) in (int, float) and isfinite(value) else None


def fresh(value, now, seconds=90):
    stamp = parse_provider_time(value)
    return bool(stamp and stamp.astimezone(CN).date() == now.date()
                and 0 <= (now-stamp).total_seconds() <= seconds)


def build_actions(cfg, state, book, *, now=None):
    now = (now or datetime.now(CN)).astimezone(CN)
    health = state.get('quote_health') or {}
    valid = bool(not state.get('stale') and not state.get('config_mismatch') and state.get('mode') == 'live'
                 and state.get('market_session') and state.get('calendar_ok') and now.weekday() < 5
                 and (time(9,30) <= now.time() < time(11,30) or time(13) <= now.time() < time(15))
                 and fresh(state.get('fetch_finished_at'), now, 45)
                 and (state.get('risk') or {}).get('valid') and health.get('all_held_usable') is True
                 and not health.get('block_trade_signals'))
    signals = allowed_signals(cfg, state.get('trade_signals') or []) if valid else []
    quotes = {str(q['code']): q for q in state.get('quotes', [])}
    signals = [s for s in signals if s.get('code')=='PORT' or
               fresh(quotes.get(s.get('code'),{}).get('source_timestamp'),now)]
    holdings = {str(p['code']): p for p in cfg.get('positions', []) if p.get('quantity', 0) > 0}
    names = {str(p['code']): p for p in cfg.get('watchlist', [])}
    names.update(holdings)
    codes = list(holdings) + [s['code'] for s in signals if s.get('category') == 'add' and s['code'] not in holdings]
    cards = []
    rank = {'reduce': 0, 't': 1, 'add': 2}
    for code in dict.fromkeys(codes):
        p = names.get(code, {'name': code});q = quotes.get(code, {})
        usable = valid and fresh(q.get('source_timestamp'), now) and number(q.get('price')) is not None and q['price'] > 0
        options = sorted([s for s in signals if s['code'] == code], key=lambda s: rank.get(s.get('category'), 3)) if usable else []
        selected = options[0] if options else None
        plan = (cfg.get('trade_plans') or {}).get(code) or {}
        inventory = (book.get('availability') or {}).get(code, {})
        available = inventory.get('available_quantity')
        checked = bool(inventory.get('date') == str(now.date()) and inventory.get('base_quantity') == p.get('quantity')
                       and type(available) is int and 0 <= available <= p.get('quantity', 0))
        card = {'code': code, 'name': p.get('name', code), 'state': 'wait' if usable else 'paused',
                'label': '等待条件' if usable else '暂停实时判断', 'price': q.get('price') if usable else None,
                'quote_at': q.get('source_timestamp'), 'quantity': None, 'cash_after': None, 'fee': None,
                'available_quantity': available if checked else None, 'inventory_at': inventory.get('confirmed_at'),
                'alert_id': None, 'reason': '当前没有满足条件的交易。' if usable else '收盘、行情过期或账户数据未核验，不能据此下单。',
                'plan': {k: plan.get(k) for k in ('trigger_price', 'entry_price', 'stop_loss', 'take_profit_1', 'take_profit_2', 'signal_date', 'valid_on')},
                'cancel': ['行情或账户数据失效即停止执行', '成交后更新持仓、现金和可卖数量'],
                'validation': '规则条件提示，尚不能据此认定策略具有盈利优势'}
        if exit_only(cfg):
            card['reason'] = '退出休息模式：暂停新增和做T接回，仅复核已有仓位的退出条件。' if usable else card['reason']
        if selected:
            category = selected.get('category');side = 'buy' if category == 'add' or selected.get('trigger') == 'buyback_ready' else 'sell'
            label = '减仓复核' if category == 'reduce' else '做T接回复核' if side == 'buy' and category == 't' else '做T高抛复核' if category == 't' else '买入复核'
            card.update(state='reduce' if side == 'sell' else 'buy',label=label,reason=selected.get('reason'),alert_id=selected.get('id'),level=selected.get('level'),side=side)
            shares = selected.get('shares')
            if type(shares) is int and shares > 0 and not selected.get('blockers'):
                if side == 'sell' and checked: card['quantity'] = min(shares, available, p['quantity']) or None
                elif side == 'buy' and (category != 't' or selected.get('tradable') is True): card['quantity'] = shares
            if side == 'sell' and not checked: card['cancel'].append('可卖数量未核验，不显示执行数量')
            if side == 'buy': card['cancel'].append('超过计划最高成交价或风控受阻，取消买入')
            else: card['cancel'].append('卖出参考价不是最低成交保证；提交前核对券商盘口')
            qty = card['quantity'];cash = number(cfg.get('cash'))
            if qty and cash is not None:
                fee, basis = estimate_leg(p.get('type', 'stock'),side,q['price']*qty,book.get('fee_model') or {},[])
                fee = max(20., fee or 0.)
                card.update(fee=round(fee,2),fee_note='按费率估算并至少预留20元；非实际费用',
                            cash_after=round(cash + (1 if side=='sell' else -1)*q['price']*qty-fee,2))
                if side=='buy' and card['cash_after'] < 0:
                    card.update(quantity=None,cash_after=None,state='paused',label='资金待核验')
                    card['cancel'].append('含费用预留后现金不足，暂停执行数量')
        cards.append(card)
    equity = number((state.get('risk') or {}).get('equity'))
    reserve = equity * (1-min(60., float((cfg.get('strategy_v1') or {}).get('max_exposure_pct',60)))/100) if valid and equity else None
    return {'valid':valid,'asof':now.isoformat(),'mode':'exit_only' if exit_only(cfg) else 'normal',
            'cards':cards,'signals':signals,'cash_floor':round(reserve,2) if reserve is not None else None,
            'note':'数量为核验上限；各卡现金为单笔独立估算，不可叠加当作组合计划。总仓现金目标不代表单只仓位已经达标。'}
