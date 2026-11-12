"""Explainable agreement gate for Chan structure and completed-bar indicators."""
from __future__ import annotations

def inspect(chan, tech, integrated):
    checks=[]
    if not chan.get('valid') or not tech.get('valid'):
        return {'decision':'证据不足：继续观察','verified':False,
                'checks':['缠论与MACD至少一项缺少合格的完整历史日K'],
                'note':'不存在自动买卖指令；不足K线不应输出严格买卖点。'}
    cd=(chan.get('interpretation') or {}).get('basis_date')
    td=tech.get('asof')
    same=bool(cd and td and cd==td)
    checks.append(('通过' if same else '不通过')+f'：分析日期一致性（日线 {td} / 缠论 {cd or "未提供"}）')
    macd=tech.get('macd',{})
    checks.append('MACD：'+str(macd.get('status','缺失')))
    vol=tech.get('volume',{})
    checks.append('成交量：'+str(vol.get('status','缺失')))
    checks.append('均线：'+str(tech.get('trend','未知')))
    checks.append('缠论：'+str((chan.get('interpretation') or {}).get('phase','未知阶段')))
    # A structure candidate is not a strict 1/2/3 buy or sell point.
    action=integrated.get('action','继续观察')
    conflict=('冲突' in integrated.get('alignment',''))
    verified=bool(same and not conflict and integrated.get('alignment') in ('偏强证据一致','偏弱证据一致'))
    if not same or conflict:decision='信号不一致：继续观察';verified=False
    elif action=='买入条件观察':decision='偏强条件一致，但不能直接买入'
    elif '减仓' in action:decision='偏弱条件一致，优先核对风险'
    else:decision='观察，等待结构和量能进一步确认'
    return {'decision':decision,'verified':verified,'checks':checks,
            'note':'仅验证本级别历史日K结构和MACD/量价的一致性，不构成严格次级别背驰或一二三类买卖点认证，也未做收益率回测。'}
