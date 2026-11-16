#!/bin/bash
set -u
cd "$(dirname "$0")" || exit 1
printf '\n%s\n' '=== 衡势 V3.6.1 离线回归测试 ==='
APP="$HOME/Library/Application Support/AshareAB/app"
PY="$APP/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  source ./python_scan.sh
  if ! find_compatible_python; then
    echo '找不到兼容 Python。请先运行安装程序。'
    read -r -p '按回车关闭窗口... ' _
    exit 1
  fi
  PY="$FOUND_PYTHON"
fi
RESULT=0
for t in test_action_center.py test_action_ui.py test_t_performance.py test_v1_backtest_data.py test_v1_backtest_report.py test_v1_backtest.py \
  test_v1_validation.py test_v1_history_import.py test_v1_tencent_archive.py \
  test_trade_reconciliation.py test_alert_journal.py \
  test_close_review.py test_paper_runtime.py test_paper_view.py test_paper_contest.py test_asset_history_tencent.py test_close_refresh.py test_suggested_exit.py test_suggested_exit_api.py test_suggested_ui.py test_t_quotes.py test_t_engine.py test_t_runtime.py test_t_ui.py test_price_first.py test_csrf_recovery.py test_strategy_runtime.py test_strategy_risk.py test_strategy_setup.py test_strategy_ui.py test_phase2_strategy.py test_swing_analysis.py test_account_snapshot.py test_tencent_quotes.py test_rule_guards.py test_v361.py test_v360.py test_chan_daily.py test_chan_history.py test_chan_plain.py test_intraday.py test_market_context.py test_pnl.py test_poll_cadence.py test_portfolio_quotes.py test_technical_engine.py test_trade_signals.py; do
  printf '\n--- %s ---\n' "$t"
  "$PY" "$t" || RESULT=1
done
if [[ $RESULT -eq 0 ]]; then echo '全部离线测试通过（不代表实时行情源已验证）。'; else echo '部分离线测试未通过，请复制失败内容反馈。'; fi
read -r -p '按回车关闭窗口... ' _
exit "$RESULT"
