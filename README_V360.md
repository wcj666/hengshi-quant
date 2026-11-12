# 衡势 A股量化助手 V3.6.1

本地A股研究面板（仅研究、风控估算，不连接券商、不自动交易）。

- 盘中目标10秒抓取持仓，网页每6秒检查；自选观察约60秒刷新，海外和竞价独立执行。
- 行情源时间戳、完整性、跳价和疑似停滞检测。
- 50%、80%、100%账户回撤预警，保留组合压力风险计算。
- 按新行情做假设减仓模拟，不改变账户。
- 人工真实成交日志及前瞻估算净值曲线，不能代表正式账户业绩。
- 历史缠论结构、MACD(12/26/9)、量价、均线交叉证据解释（非严格买卖点）。

## 快速开始

Mac：解压后双击 `安装_开机自动启动.command`，打开 `http://127.0.0.1:8765/`，核验版本 **3.6.1**。

开发环境：`python -m pip install -r requirements.txt`；`python serve_web.py` 启动Web，另一个终端 `python intraday_monitor.py --mode live --interval 10` 启动监控。

## 离线测试

`python test_v360.py`；`python test_chan_history.py`；`python test_technical_engine.py`；`python test_trade_signals.py`；`python test_pnl.py`；`node --check static/dashboard.js`。

详情参见 [`V3.6.1_更新说明.md`](V3.6.1_更新说明.md)。
