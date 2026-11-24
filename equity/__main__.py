"""命令行入口：python3 -m equity <子命令> [台账目录]。

子命令：
  audit     核对台账格式与语义约束
  report    服务公平复盘（分层实测、标准化水准、差距、趋势、特别分组）
  allocate  下一季改进额度复算
  appeals   申诉重算对照（基线 vs 计入申诉，含缺证保护）
  query     经办人回查指定季与分区/群组的指标适用范围、额度变动与依据
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from . import ledger as ledger_mod
from . import report as report_mod


def _emit(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _load(location: Path):
    ledger, issues = ledger_mod.load(location)
    return ledger, issues


def _budget_season(ledger, season: str | None) -> str:
    if season:
        return season
    seasons = [b.season for b in ledger.budgets]
    if not seasons:
        raise SystemExit("台账中没有额度记录，请用 --season 指定或检查 budget.csv")
    return max(seasons, key=ledger.season_index)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="equity", description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data"), help="台账目录（默认 ./data）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("audit", help="核对台账格式与语义约束")

    p_report = sub.add_parser("report", help="服务公平复盘")
    p_report.add_argument("--season", help="只输出指定季")
    p_report.add_argument(
        "--scenario", choices=["baseline", "appealed"], default="appealed", help="归属情景"
    )

    p_alloc = sub.add_parser("allocate", help="下一季改进额度复算")
    p_alloc.add_argument("--season", help="额度所属季（默认取额度台账中的最后一季）")
    p_alloc.add_argument(
        "--scenario", choices=["baseline", "appealed"], default="appealed", help="归属情景"
    )

    p_appeals = sub.add_parser("appeals", help="申诉重算对照")
    p_appeals.add_argument("--season", help="额度所属季（默认取额度台账中的最后一季）")

    p_query = sub.add_parser("query", help="回查指标适用范围、额度变动与依据")
    p_query.add_argument("--season", required=True)
    p_query.add_argument("--village")
    p_query.add_argument("--group")

    args = parser.parse_args(argv)

    if args.command == "audit":
        import input_audit

        counts, format_issues = input_audit.audit(args.data)
        ledger, semantic_issues = _load(args.data) if not format_issues else (None, [])
        issues = list(format_issues) + list(semantic_issues)
        _emit({"源表行数": counts, "问题": issues})
        return int(bool(issues))

    ledger, issues = _load(args.data)
    if issues:
        _emit({"问题": issues})
        return 1

    if args.command == "report":
        if args.season and args.season not in ledger.seasons:
            _emit({"问题": [f"未知季 {args.season}，可选 {list(ledger.seasons)}"]})
            return 1
        _emit(report_mod.full_report(ledger, args.scenario == "appealed", args.season))
        return 0

    if args.command == "allocate":
        season = _budget_season(ledger, args.season)
        _emit(report_mod.allocation_report(ledger, season, args.scenario == "appealed"))
        return 0

    if args.command == "appeals":
        season = _budget_season(ledger, args.season)
        _emit(report_mod.appeal_report(ledger, season))
        return 0

    if args.command == "query":
        if args.season not in ledger.seasons:
            _emit({"问题": [f"未知季 {args.season}，可选 {list(ledger.seasons)}"]})
            return 1
        if args.village and args.village not in ledger.villages:
            _emit({"问题": [f"未知分区 {args.village}，可选 {list(ledger.villages)}"]})
            return 1
        if args.group and args.group not in ledger.groups:
            _emit({"问题": [f"未知群组 {args.group}，可选 {sorted(ledger.groups)}"]})
            return 1
        _emit(report_mod.query_report(ledger, args.season, args.village, args.group))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
