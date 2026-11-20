"""Browser preview and confirmation for the independent simulated ledger."""
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import fcntl
import json
from pathlib import Path
import re

from itsdangerous import BadData, URLSafeTimedSerializer

import paper_contest as engine

PREVIEW_SECONDS = 30


class PaperTradeConflict(ValueError):
    pass


def trade_input(body):
    if not isinstance(body, dict):
        raise ValueError('请填写模拟交易资料')
    code, side, quantity, reason = (body.get(key) for key in ('code', 'side', 'quantity', 'reason'))
    if not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code) or side not in ('buy', 'sell'):
        raise ValueError('请选择买卖方向并填写六位证券代码')
    if type(quantity) is not int or not 0 < quantity <= 100_000_000:
        raise ValueError('请输入大于零的整数数量')
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 300:
        raise ValueError('请填写模拟操作原因，最多 300 字')
    return {'code': code, 'side': side, 'quantity': quantity, 'reason': reason.strip()}


def _read(store):
    try:
        state = json.loads((store / 'state.json').read_text(encoding='utf-8'))
        if not isinstance(state, dict):
            raise ValueError('模拟账本格式无效')
        engine.validate_state(state)
        engine.refresh_continuation_calendar(state, store, engine.clock_now())
        engine.validate_state(state)
        return state
    except (ValueError, KeyError, TypeError, OverflowError) as error:
        raise PaperTradeConflict('模拟账本校验未通过') from error


@contextmanager
def _locked(store):
    with (store / 'paper.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PaperTradeConflict('模拟账本正在刷新，请几秒后重试') from error
        yield


def _revision(state):
    # Quote observations replace state.json every 30 seconds. They don't change
    # the holdings, cash or trade sequence reviewed by the user.
    fields = {key: state[key] for key in ('fixed_rules_hash', 'cash', 'positions', 'plans', 'trades')}
    fields['continuation_hash'] = state.get('continuation_hash')
    encoded = json.dumps(fields, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    return sha256(encoded).hexdigest()


class PaperOperations:
    def __init__(self, store, secret):
        self.store = Path(store)
        self.signer = URLSafeTimedSerializer(secret, salt='paper-contest-trade-preview')

    def preview(self, body):
        trade = trade_input(body)
        state = _read(self.store)
        engine._session(state, engine.clock_now(), trade['side'])
        quotes, _ = engine.fetch_quotes(state)
        estimate = engine.preview_trade(state, quotes=quotes, now=engine.clock_now(), **trade)
        if not estimate['allowed']:
            return {'ok': False, 'error': estimate['error'], 'preview': estimate}
        token = self.signer.dumps({'trade': trade, 'revision': _revision(state),
                                   'price': estimate['price'], 'cash_after': estimate['cash_after']})
        estimate.update(quantity=trade['quantity'], fee_total=estimate['fee']['total'],
                        source_asof=estimate['quote_asof'])
        return {'ok': True, 'preview': estimate, 'token': token, 'expires_in': PREVIEW_SECONDS}

    def _confirmation(self, token):
        try:
            return self.signer.loads(token, max_age=PREVIEW_SECONDS)
        except BadData as error:
            raise PaperTradeConflict('预览已过期或无效，请重新预览') from error

    def confirm(self, body):
        if not isinstance(body, dict) or not isinstance(body.get('token'), str) or len(body['token']) > 4096:
            raise ValueError('请先预览这笔模拟操作')
        trade = trade_input(body.get('trade'))
        token = body['token']
        preview = self._confirmation(token)
        if preview['trade'] != trade:
            raise PaperTradeConflict('操作内容已变化，请重新预览')
        request_id = sha256(token.encode()).hexdigest()
        state = _read(self.store)
        previous = next((row for row in state['trades'] if row.get('preview_id') == request_id), None)
        if previous:
            return previous, True, None
        if _revision(state) != preview['revision']:
            raise PaperTradeConflict('模拟账本已变化，请重新预览')
        engine._session(state, engine.clock_now(), trade['side'])
        quotes, _ = engine.fetch_quotes(state)

        # Network I/O takes place before the lock. Re-check under the same lock
        # used by the quote worker and CLI before saving any fill.
        with _locked(self.store):
            self._confirmation(token)
            current = _read(self.store)
            previous = next((row for row in current['trades'] if row.get('preview_id') == request_id), None)
            if previous:
                return previous, True, None
            if _revision(current) != preview['revision']:
                raise PaperTradeConflict('模拟账本已变化，请重新预览')
            now = engine.clock_now()
            working = deepcopy(current)
            try:
                fill = engine.execute(working, quotes=quotes, now=now, **trade)
            except ValueError as error:
                raise PaperTradeConflict(str(error)) from error
            worse_price = fill['price'] > preview['price'] if trade['side'] == 'buy' else fill['price'] < preview['price']
            if worse_price or working['cash'] < preview['cash_after']:
                raise PaperTradeConflict('最新盘口的成交价或回款劣于预览，请重新预览')

            fill['preview_id'] = request_id
            warning = None
            try:
                engine.observe(working, quotes, now)
            except ValueError as error:
                warning = '虚拟成交已记录，完整持仓估值待刷新'
                working['events'].append({'at': now.isoformat(), 'kind': 'valuation_pending', 'reason': str(error)})
            engine.validate_state(working)
            engine._write(self.store / 'state.json', working)
            try:
                (self.store / 'REPORT.md').write_text(engine.report(working), encoding='utf-8')
            except OSError:
                warning = '虚拟成交已记录，报告待后台刷新'
            return fill, False, warning
