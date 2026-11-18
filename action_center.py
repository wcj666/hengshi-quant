"""Build action cards from verified live data and the existing trading rules."""
from datetime import datetime, time, timedelta
from math import isfinite
from zoneinfo import ZoneInfo

from quote_health import parse_provider_time
from strategy_v1 import strategy_prefs
from t_engine import T_CUTOFF
from t_costs import cash_impact

CN = ZoneInfo('Asia/Shanghai')
EXIT_LEVELS = {
    'stop': ('固定止损', 'at_or_below'),
    'take_profit_1': ('第一止盈', 'at_or_above'),
    'take_profit_2': ('第二止盈', 'at_or_above'),
    'breakdown': ('跌破观察位', 'at_or_below'),
}


def exit_only(cfg):
    return (cfg.get('execution_preferences') or {}).get('mode') == 'exit_only'


def allowed_signals(cfg, signals):
    return [s for s in signals if not exit_only(cfg) or s.get('category') == 'reduce' or s.get('ledger_notice') is True]


def number(value):
    return value if type(value) in (int, float) and isfinite(value) else None


def positive(value):
    value = number(value)
    return value if value is not None and value > 0 else None


def fresh(value, now, seconds=90):
    stamp = parse_provider_time(value)
    return bool(stamp and stamp.astimezone(CN).date() == now.date()
                and 0 <= (now - stamp).total_seconds() < seconds)


def market_status(state, now):
    clock = now.time()
    in_session = now.weekday() < 5 and (time(9, 30) <= clock < time(11, 30) or time(13) <= clock < time(15))
    health = state.get('quote_health') or {}
    if not in_session:
        return '非交易时段，暂停实时提醒'
    if not state.get('mode'):
        return '未收到后台监控数据，请检查监控进程'
    if state.get('config_mismatch'):
        return '持仓或设置已变化，等待行情按新台账重算'
    if state['mode'] != 'live':
        return '当前行情仅供参考，暂停实时提醒'
    if state.get('stale') or not fresh(state.get('fetch_finished_at'), now, 45):
        return '行情更新超时，请检查后台监控与网络'
    if not state.get('calendar_ok'):
        return '交易日历尚未核验，暂停提醒'
    if not state.get('market_session'):
        return '尚未收到连续交易时段的行情'
    if health.get('all_held_usable') is not True or health.get('block_trade_signals'):
        return '持仓报价或源时间未通过校验，暂停提醒'
    if not (state.get('risk') or {}).get('valid'):
        return '账户风险数据未核验，暂停提醒'
    return None


def threshold(label, direction, level, price, cap=None):
    """Positive distance is the move still needed to reach the threshold."""
    level = positive(level)
    if level is None:
        return None
    distance = None if price is None else round(
        level - price if direction == 'at_or_above' else price - level, 8)
    return {'label': label, 'direction': direction, 'price': level, 'distance': distance,
            'max_price': positive(cap)}


def card_thresholds(plan, quote, signal, price, held):
    if not held:
        level = threshold('买入触发', 'at_or_above', plan.get('trigger_price'), price, plan.get('entry_price'))
        return [level] if level else []
    review = quote.get('strategy_exit') or {}
    levels = {'stop': review.get('stop_loss') or plan.get('stop_loss'),
              'take_profit_1': plan.get('take_profit_1'), 'take_profit_2': plan.get('take_profit_2')}
    trigger = (signal or {}).get('trigger')
    if trigger in EXIT_LEVELS:
        levels[trigger] = signal.get('level')
    return [row for kind, level in levels.items()
            if (row := threshold(*EXIT_LEVELS[kind], level, price))]


def signal_applicable(signal, quote, plans, now):
    """Discard signals whose price boundary or data has ceased to apply."""
    code = signal['code']
    if code == 'PORT':
        return True
    price = positive(quote.get('price'))
    if price is None or not fresh(quote.get('source_timestamp'), now):
        return False
    trigger = signal.get('trigger')
    level = positive(signal.get('level'))
    if trigger in EXIT_LEVELS:
        direction = EXIT_LEVELS[trigger][1]
        return level is not None and (price <= level if direction == 'at_or_below' else price >= level)
    if trigger == 'v1_entry':
        plan = plans.get(code) or {}
        cap = positive(plan.get('entry_price'))
        opened = positive(quote.get('open', quote.get('open_price')))
        valid_on = plan.get('valid_on')
        return ((not valid_on or valid_on == str(now.date())) and opened is not None and cap is not None
                and opened <= cap and time(9, 45) <= now.time() and level is not None
                and level <= price <= cap and quote.get('at_limit_up') is not True
                and quote.get('limit_status_verified') is not False)
    if trigger in ('sell_ready', 'buyback_ready'):
        if ((signal.get('tradable') is not True and signal.get('validation_status') not in ('pending', 'excluded'))
                or now.time() >= T_CUTOFF):
            return False
        if trigger == 'buyback_ready':
            return level is not None and price <= level
    return True


def t_card(row, cycle, price, mode):
    if not row and not cycle:
        return None
    row = row or {}
    remaining = cycle.get('unbought_quantity') if cycle else row.get('remaining_quantity')
    result = {'status': row.get('status') or '等待最新分时及台账核验',
              'reason': row.get('reason'), 'cycle': bool(cycle or row.get('cycle_id')),
              'remaining_quantity': remaining,
              'sell_price': cycle.get('sell_price') if cycle else row.get('sell_price'),
              'checks': [c for c in row.get('checks', []) if c.get('passed') is not True
                         and (not (cycle or row.get('cycle_id')) or c.get('label') != '当日可卖底仓')]}
    if mode == 'exit_only':
        result.update(status='退出休息，暂停做 T', reason=None, checks=[])
        return result
    if price is None or not row:
        return result
    result.update({k: row.get(k) for k in ('break_even_quantity', 'break_even_gap',
                  'minimum_spread', 'estimated_roundtrip_fee', 'fee_basis', 'minimum_gross',
                  'extra_cost_buffer', 'vwap', 'opening_price_floor')})
    cap = positive(row.get('target_buy')) if result['cycle'] else None
    if cap is not None:
        result.update(buyback_cap=cap, distance_to_cap=round(max(0, price - cap), 8))
    return result


def build_actions(cfg, state, book, *, now=None, output=None):
    now = (now or datetime.now(CN)).astimezone(CN)
    pause = market_status(state, now)
    valid = pause is None
    mode = 'exit_only' if exit_only(cfg) else 'normal'
    quotes = {str(q['code']): q for q in state.get('quotes', [])}
    plans = cfg.get('trade_plans') or {}
    holdings = {str(p['code']): p for p in cfg.get('positions', []) if p.get('quantity', 0) > 0}
    watches = {str(p['code']): p for p in cfg.get('watchlist', [])}
    observation = state.get('t_observe') or {}
    t_current = (valid and book.get('revision') is not None
                 and observation.get('ledger_revision') == book['revision']
                 and fresh(observation.get('asof'), now, 45))
    t_rows = {str(row['code']): row for row in observation.get('rows', [])} if t_current else {}
    t_deadlines = {}
    for code, row in t_rows.items():
        if fresh(row.get('minute_source_at'), now) and fresh(row.get('minute_asof'), now, 150):
            t_deadlines[code] = min(parse_provider_time(observation['asof']) + timedelta(seconds=45),
                                    parse_provider_time(row['minute_source_at']) + timedelta(seconds=90),
                                    parse_provider_time(row['minute_asof']) + timedelta(seconds=150))
    cycles_by_code = {}
    for cycle in book.get('cycles', []):
        if cycle.get('status') == 'open' and cycle.get('unbought_quantity', 0) > 0:
            cycles_by_code.setdefault(str(cycle['code']), []).append(cycle)
    signals = allowed_signals(cfg, [s for s in state.get('trade_signals') or [] if not s.get('ledger_notice')]) if valid else []
    signals = [s for s in signals if (s.get('category') != 't' or s['code'] in t_deadlines)
               and signal_applicable(s, quotes.get(s['code'], {}), plans, now)]
    if output is not None:
        from alert_journal import followup_signals
        from reminder_policy import apply_policy
        signals = apply_policy(output, cfg, signals, now=now)
        signals = followup_signals(output, signals, cfg.get('positions', []), book.get('availability') or {},
                                   cfg.get('reconciled_trades', []), now=now)
    from t_engine import closeout_signals
    ledger_signals = closeout_signals(cfg, book, now=now,
        calendar_ok=state.get('calendar_ok') is True,
        market_session=(state.get('mode') == 'live' and state.get('market_session') is True
                        and fresh(state.get('fetch_finished_at'), now, 45)))
    pending = {code for code in watches if (plans.get(code) or {}).get('strategy_version') == '1.0'}
    codes = list(dict.fromkeys([*holdings, *(code for code in watches if code in pending),
                                *(s['code'] for s in signals if s['category'] == 'add')]))
    cycle_selection = {code: rows[0] for code, rows in cycles_by_code.items() if len(rows) == 1}
    cycle_end = now.replace(hour=11, minute=30, second=0, microsecond=0) if now.hour < 12 else now.replace(hour=15, minute=0, second=0, microsecond=0)
    expires = min(parse_provider_time(state['fetch_finished_at']) + timedelta(seconds=45), cycle_end) if valid else None
    cards = []
    rank = {'reduce': 0, 't': 1, 'add': 2}
    for code in codes:
        held = code in holdings
        p = (holdings if held else watches).get(code, {})
        q = quotes.get(code, {})
        usable = valid and fresh(q.get('source_timestamp'), now) and positive(q.get('price')) is not None
        price = q['price'] if usable else None
        deadline = min(expires, parse_provider_time(q['source_timestamp']) + timedelta(seconds=90)) if usable else None
        options = sorted((s for s in signals if s['code'] == code and s.get('category') in rank
                          and s.get('trigger') != 'carry_cash'), key=lambda s: rank[s['category']]) if usable else []
        selected = options[0] if options else None
        plan = plans.get(code) or {}
        inventory = (book.get('availability') or {}).get(code, {})
        available = inventory.get('available_quantity')
        checked = (held and inventory.get('date') == str(now.date())
                   and inventory.get('base_quantity') == p.get('quantity')
                   and type(available) is int and 0 <= available <= p['quantity'])
        reasons = list((q.get('strategy_exit') or {}).get('pending', [])) if held and usable else []
        if held and usable and not (q.get('strategy_exit') or {}).get('stop_loss') and not plan.get('stop_loss'):
            reasons.append('固定止损待补')
        guidance = '等待价格触发固定计划或出现已核验的结构条件。'
        if usable and code in pending and not held:
            from strategy_setup import evaluate_entry
            if not selected:
                entry = evaluate_entry(cfg, code, q, state, now=now)
                reasons = entry['blockers']
            guidance = '9:45 后，价格进入计划区间并通过形态与账户核验；高开超过最高接受价时取消全天计划。'
        elif held and usable and not checked:
            reasons.append('当日券商可卖数量未核验')
        elif held and usable and available == 0:
            reasons.append('券商记录当日可卖数量为 0')
        t_row = t_rows.get(code)
        t_expiry = min(deadline, t_deadlines[code]) if deadline and code in t_deadlines else None
        if t_row and t_row.get('minute_asof') and t_expiry is None:
            t_row = {'status': '分时或报价已过期，等待重新核验'}
        t = t_card(t_row, cycle_selection.get(code), price if t_expiry else None, mode)
        if t and t_expiry:
            t['expires_at'] = t_expiry.isoformat()
        if held and t is None and (cfg.get('t_settings') or {}).get('enabled') is True:
            t = t_card({'status': '等待分时与做 T 台账重新核验'}, None, None, mode)
        t_cutoff = datetime.combine(now.date(), T_CUTOFF, CN)
        if t and t.get('buyback_cap') and t_expiry:
            t['expires_at'] = min(t_expiry, t_cutoff).isoformat()
        card = {'code': code, 'name': p.get('name', code),
                'state': 'wait' if usable else 'paused',
                'label': ('待核条件' if reasons else '条件未触发') if usable else '暂停实时判断',
                'price': price, 'quote_at': q.get('source_timestamp'), 'quantity': None,
                'cash_after': None, 'fee': None, 'alert_id': None,
                'available_quantity': available if checked else None,
                'inventory_at': inventory.get('confirmed_at') if checked else None,
                'reason': guidance if usable else pause or '个股报价缺失、过期或源时间未核验',
                'plan': {k: plan.get(k) for k in ('trigger_price', 'entry_price', 'stop_loss',
                                                 'take_profit_1', 'take_profit_2', 'signal_date', 'valid_on')},
                'thresholds': card_thresholds(plan, q if usable else {}, selected, price, held),
                'blockers': list(dict.fromkeys(reasons)), 't': t,
                'expires_at': deadline.isoformat() if deadline else None,
                'cancel': ['行情过期或台账变化，重新核验后才显示数量'],
                'validation': '规则条件提示，尚不能据此认定策略具有盈利优势'}
        if mode == 'exit_only' and usable and not selected:
            card['reason'] = '退出休息模式，仅复核已有仓位的退出条件。'
        if selected:
            category = selected['category']
            trigger = selected.get('trigger')
            buy = category == 'add' or trigger == 'buyback_ready'
            shares = selected.get('shares')
            observation_only = category == 't' and trigger not in ('sell_ready', 'buyback_ready')
            observation_only |= category == 'reduce' and trigger in ('structure_support', 'structure_weakness', 'resistance_test', 'exit_review')
            observation_only |= selected.get('validation_status') in ('pending', 'excluded') or selected.get('followup_status') == 'waiting_inventory'
            label = ('买入复核' if category == 'add' else '做 T 接回复核' if buy else
                     '结构观察' if observation_only and category == 'reduce' else
                     '做 T 条件观察' if observation_only else '做 T 高抛复核' if category == 't' else '卖出复核')
            if selected.get('followup'):
                label = '剩余数量待核' if observation_only else '剩余减仓复核'
            elif selected.get('validation_status') in ('pending', 'excluded'):
                label = '待验证观察' if selected['validation_status'] == 'pending' else '已停用强提醒'
            card.update(state='wait' if observation_only else 'buy' if buy else 'reduce',
                        label=label, alert_id=selected.get('id'), reason=selected.get('reason'),
                        side=None if observation_only else 'buy' if buy else 'sell')
            card.update({key: selected.get(key) for key in ('validation_status', 'validation_reason',
                        'followup', 'original_target', 'filled_before', 'remaining_target')})
            card['blockers'] = list(dict.fromkeys(card['blockers'] + (selected.get('blockers') or [])))
            if not observation_only and type(shares) is int and shares > 0 and not selected.get('blockers'):
                if buy:
                    card['quantity'] = shares
                elif checked and available:
                    card['quantity'] = min(shares, available)
            if category == 't' and t_expiry:
                card['expires_at'] = min(t_expiry, t_cutoff).isoformat()
            if buy:
                card['cancel'].append('价格超出计划区间、资金不足或风控受阻即取消')
            else:
                card['cancel'].append('退出触发价用于判断条件，实际成交价取决于券商盘口')
            qty = card['quantity']
            cash = number(cfg.get('cash'))
            if qty and cash is not None:
                side = card['side']
                import t_runtime
                impact = cash_impact(p.get('type', 'stock'), side, price * qty,
                                    t_runtime.settings(cfg, book)['fee_reserve'],
                                    book.get('fee_model'), book.get('fee_samples') or [])
                card.update(fee=impact['estimated_fee'], estimated_fee=impact['estimated_fee'],
                            safety_reserve=impact['safety_reserve'], reserved_cost=impact['reserved_cost'],
                            fee_note='预计手续费与额外安全预留分别列示；预留仍属于账户资产',
                            cash_after=round(cash + impact['conservative_cash_delta'], 2),
                            estimated_cash_after=round(cash + impact['estimated_cash_delta'], 2)
                                if impact['estimated_cash_delta'] is not None else None)
                if buy and card['cash_after'] < 0:
                    card.update(quantity=None, cash_after=None, state='paused', label='含费用后现金不足')
                    card['blockers'].append('含费用预留后现金不足')
        cards.append(card)
    equity = positive((state.get('risk') or {}).get('equity'))
    limits = strategy_prefs(cfg)
    reserve = max(equity * (1 - limits['max_exposure_pct'] / 100),
                  limits['fee_reserve'] + 2 * limits['per_trade_risk']) if valid and equity else None
    signal_expiries = {}
    for signal in signals:
        code = signal['code']
        deadline = expires if code == 'PORT' else min(
            expires, parse_provider_time(quotes[code]['source_timestamp']) + timedelta(seconds=90))
        if signal.get('category') == 't':
            deadline = min(deadline, t_deadlines[code], datetime.combine(now.date(), T_CUTOFF, CN))
        signal_expiries[signal['id']] = deadline.astimezone(CN).isoformat()
    for signal in ledger_signals:
        signal_expiries[signal['id']] = min(parse_provider_time(signal['expires_at']),
            parse_provider_time(state['fetch_finished_at']) + timedelta(seconds=45)).astimezone(CN).isoformat()
    return {'valid': valid, 'asof': now.isoformat(), 'mode': mode, 'pause_reason': pause,
            'expires_at': expires.isoformat() if expires else None,
            'cards': cards, 'signals': signals + ledger_signals, 'closeouts': ledger_signals,
            'signal_expires_at': signal_expiries,
            'cash_floor': round(reserve, 2) if reserve is not None else None,
            'note': '各卡现金为单笔估算，不能相加作为组合计划。'}
