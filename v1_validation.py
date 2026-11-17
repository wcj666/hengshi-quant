"""Chronological, fixed-policy evaluation of three independent V1 replays."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from v1_backtest import ROOT, run_backtest
from v1_backtest_data import demo_bundle, load_bundle, validate_bundle
from v1_backtest_report import save_report


PERIODS = (
    ("development", "开发区间"),
    ("validation", "验证区间"),
    ("holdout", "留出区间"),
)
PERIOD_KEYS = tuple(key for key, _ in PERIODS)


def parse_window(value):
    if not isinstance(value, str) or value.count(":") != 1:
        raise argparse.ArgumentTypeError("区间格式为 YYYY-MM-DD:YYYY-MM-DD")
    start, end = value.split(":")
    return start, end


def check_windows(windows, calendar):
    if set(windows) != set(PERIOD_KEYS):
        raise ValueError("需要分别提供开发、验证和留出三个区间")
    indices = {day: index for index, day in enumerate(calendar)}
    previous_end = None
    checked = []
    gaps = []
    for key, label in PERIODS:
        window = windows[key]
        if not isinstance(window, (tuple, list)) or len(window) != 2:
            raise ValueError(f"{label}须包含起止两个交易日")
        start, end = window
        if any(not isinstance(day, str) or day not in indices for day in (start, end)):
            raise ValueError(f"{label}的起止日期须在数据包交易日历内")
        if indices[start] > indices[end]:
            raise ValueError(f"{label}起始日期晚于结束日期")
        if previous_end is not None:
            first_gap_day = indices[previous_end] + 1
            gap_size = indices[start] - first_gap_day
            if gap_size < 0:
                raise ValueError("开发、验证和留出区间必须按时间顺序且不可重叠")
            if gap_size:
                gaps.append({
                    "after": previous_end, "before": start, "start": calendar[first_gap_day],
                    "end": calendar[indices[start] - 1], "session_count": gap_size,
                })
        checked.append((key, label, start, end))
        previous_end = end
    return checked, gaps


def period_warnings(report):
    summary = report["summary"]
    coverage = report["meta"].get("input_coverage", {})
    warnings = []
    if not summary["execution_coverage_complete"]:
        warnings.append("执行证据不完整，部分信号或成交无法验证")
    if not summary["valuations_complete"]:
        warnings.append("日末估值缺失，区间净值及其衍生指标可能不可计算")
    if any(coverage.get(field) for field in (
        "missing_sessions", "partial_minute_sessions", "corporate_action_unverified_sessions",
        "daily_unready_sessions", "benchmark_unready_days", "universe_missing_days",
    )):
        warnings.append("数据覆盖审计发现缺口，请查看缺失场次和日期")
    if summary["session_count"] < 20 or summary["closed_trade_count"] < 30:
        warnings.append("交易日少于 20 或已结束交易少于 30 笔，样本偏少")
    if report["open_positions"]:
        warnings.append("区间末仍有持仓，按当日可核验估值，仓位不转入后续区间")
    return warnings


def evaluate(bundle, windows, initial_cash=100000.0, *, bundle_sha256=None, baseline_code=None):
    """Replay disjoint windows with unchanged rules, each starting from cash."""
    validate_bundle(bundle)
    checked, gaps = check_windows(windows, bundle["calendar"])
    if bundle_sha256 is None:
        raw = json.dumps(bundle, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode()
        bundle_sha256 = hashlib.sha256(raw).hexdigest()
    periods, reports = [], {}
    common = None
    for key, label, start, end in checked:
        report = run_backtest(bundle, start, end, initial_cash, bundle_sha256=bundle_sha256)
        meta = report["meta"]
        verified = {field: meta[field] for field in (
            "bundle_sha256", "engine_sha256", "initial_cash", "rules", "costs",
        )}
        if meta["bundle_sha256"] != bundle_sha256 or common is not None and verified != common:
            raise ValueError("区间间的数据指纹、初始资金或回放规则不一致")
        common = verified
        meta["evaluation_period"] = key
        periods.append({
            "key": key, "label": label, "start": start, "end": end, "summary": report["summary"],
            "open_position_count": len(report["open_positions"]),
            "input_coverage": meta.get("input_coverage", {}), "warnings": period_warnings(report),
        })
        reports[key] = report
    demo = bundle.get("demo") is True
    policy = "三个区间按时间分开，分别从相同初始现金开始，使用同一套 V1 规则与费用参数。区间之间独立核算，不跨段携带持仓。"
    warnings = ["日期互不重叠只能核对切分，留出区间在选规则前是否已被查看或用于调参，需要由研究记录证明。"]
    if demo:
        warnings.append("这是合成样例，结果仅用于检查回放和区间对比。")
    if gaps:
        warnings.append("区间之间有交易日未纳入对比，详见间隔记录。")
    if "universe" not in bundle or not bundle.get("universe_source"):
        warnings.append("历史股票池或其出处缺失，可能存在幸存者偏差。")
    if not bundle.get("input_digests"):
        warnings.append("数据包带整体指纹，各输入文件的单独指纹缺失。")
    if sum(period["summary"]["closed_trade_count"] for period in periods) < 100:
        warnings.append("三段合计不足 100 笔已结束交易。稳定性还需结合更大样本及不同市场环境评估。")
    result = {
        "meta": {
            "demo": demo, "source": bundle["source"], "calendar_source": bundle["calendar_source"],
            "universe_source": bundle.get("universe_source"), "input_digests": bundle.get("input_digests", {}),
            "policy": policy, "warnings": warnings, "gaps": gaps, **common,
        },
        "periods": periods, "reports": reports,
    }

    from reminder_validation import evaluate_reminders

    result["reminder_validation"] = evaluate_reminders(bundle, result, baseline_code=baseline_code)
    return result


def save_comparison(result, output_dir):
    """Publish an immutable set of reports before pointing the comparison to it."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    run_id = uuid4().hex
    with TemporaryDirectory(prefix=".validation-", dir=out) as staging:
        stage = Path(staging)
        run = stage / run_id
        run.mkdir()
        for key in PERIOD_KEYS:
            save_report(result["reports"][key], run / key)
        if "reminder_validation" in result:
            (run / "reminder_validation.json").write_text(
                json.dumps(result["reminder_validation"], ensure_ascii=False, allow_nan=False, indent=2) + "\n",
                encoding="utf-8",
            )
        compare = stage / "comparison.json"
        public = {"meta": {**result["meta"], "run_id": run_id}, "periods": result["periods"]}
        compare.write_text(json.dumps(public, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
                           encoding="utf-8")
        runs = out / "runs"
        runs.mkdir(exist_ok=True)
        run.rename(runs / run_id)
        return compare.replace(out / compare.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--bundle", type=Path, help="历史回放数据包")
    source.add_argument("--demo", action="store_true", help="以合成数据演示按时间切分")
    for key, label in PERIODS:
        parser.add_argument(f"--{key}", type=parse_window, help=f"{label}，YYYY-MM-DD:YYYY-MM-DD")
    parser.add_argument("--initial-cash", type=float, default=100000.0)
    parser.add_argument("--baseline-code", help="预先指定数据包中的买入持有基准；省略则使用现金基准")
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "v1_validation")
    args = parser.parse_args()
    windows = {key: getattr(args, key) for key in PERIOD_KEYS}
    try:
        if args.demo:
            if any(windows.values()):
                parser.error("--demo 已内置三个演示区间，无需另填时间")
            bundle, digest = demo_bundle(), None
            windows = {key: (day, day) for key, day in zip(PERIOD_KEYS, bundle["calendar"][-3:])}
        else:
            if not all(windows.values()):
                parser.error("--bundle 须同时提供 --development、--validation、--holdout")
            bundle, digest = load_bundle(args.bundle)
        result = evaluate(bundle, windows, args.initial_cash, bundle_sha256=digest, baseline_code=args.baseline_code)
        report_path = save_comparison(result, args.output)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps({"report": str(report_path), "demo": result["meta"]["demo"],
                      "periods": [period["key"] for period in result["periods"]]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
