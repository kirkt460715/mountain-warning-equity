"""台账载入与语义校验：把交付 CSV 读成受控的领域对象。

格式层面的核对（表头、字段数、基础外键）由仓库根目录的
``input_audit.py`` 负责；这里在此之上补充复算所需的语义约束，
例如人口区间必须有序、参考权重必须归一、申诉归属必须在候选村内。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

TABLES = ("people", "standards", "channels", "budget", "appeals", "weather", "reach")
WEATHER_STATES = ("confirmed", "no_event")
REACH_STATES = ("within", "late", "unknown")


@dataclass(frozen=True)
class Group:
    """一类服务对象。stop 非空表示接收点在该季停用，责任未变时仍在指标范围。"""

    name: str
    kind: str
    candidates: tuple[str, ...]
    low: int
    high: int
    joined: str
    stop: str | None
    responsibility: str


@dataclass(frozen=True)
class Appeal:
    """乡镇补交的归属事实。only_new_season 为真时只用于申报季及之后。"""

    ident: str
    group: str
    village: str
    reported_season: str
    only_new_season: bool


@dataclass(frozen=True)
class Storm:
    season: str
    storm: str
    village: str
    risk: str
    state: str
    deadline_min: int


@dataclass(frozen=True)
class Reach:
    storm: str
    village: str
    group_options: tuple[str, ...]
    state: str
    count: int | None
    proof: str


@dataclass(frozen=True)
class ChannelRow:
    season: str
    village: str
    channel: str
    common_point: str


@dataclass(frozen=True)
class BudgetRow:
    season: str
    village: str
    total: float
    locked: float
    share_limit: float
    lowest: float


def _read(location: Path, table: str) -> list[dict[str, str]]:
    with (location / f"{table}.csv").open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def _ordered_unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


class Ledger:
    """一套演练台账：各源表的领域对象，以及季、分区、受众类的稳定顺序。"""

    def __init__(self, location: str | Path):
        self.location = Path(location)
        records = {name: _read(self.location, name) for name in TABLES}

        self.groups: dict[str, Group] = {}
        for row in records["people"]:
            self.groups[row["group"]] = Group(
                name=row["group"],
                kind=row["kind"],
                candidates=tuple(part for part in row["village_candidates"].split("|") if part),
                low=int(row["low"]),
                high=int(row["high"]),
                joined=row["joined"],
                stop=row["stop"] or None,
                responsibility=row["responsibility"],
            )

        self.standards: dict[str, dict[tuple[str, str], float]] = {}
        for row in records["standards"]:
            self.standards.setdefault(row["standard"], {})[(row["kind"], row["risk"])] = float(
                row["weight"]
            )

        self.channels: list[ChannelRow] = [
            ChannelRow(row["season"], row["village"], row["channel"], row["common_point"])
            for row in records["channels"]
        ]

        self.budgets: list[BudgetRow] = [
            BudgetRow(
                row["season"],
                row["village"],
                float(row["total"]),
                float(row["locked"]),
                float(row["share_limit"]),
                float(row["lowest"]),
            )
            for row in records["budget"]
        ]

        self.appeals: list[Appeal] = [
            Appeal(
                row["id"],
                row["group"],
                row["village"],
                row["reported_season"],
                row["only_new_season"].strip().lower() == "true",
            )
            for row in records["appeals"]
        ]

        self.storms: list[Storm] = [
            Storm(
                row["season"],
                row["storm"],
                row["village"],
                row["risk"],
                row["state"],
                int(row["deadline_min"]),
            )
            for row in records["weather"]
        ]

        self.reaches: list[Reach] = [
            Reach(
                row["storm"],
                row["village"],
                tuple(part for part in row["group_options"].split("|") if part),
                row["state"],
                int(row["count"]) if row["count"] else None,
                row["proof"],
            )
            for row in records["reach"]
        ]

        self.seasons: tuple[str, ...] = _ordered_unique([s.season for s in self.storms])
        self.villages: tuple[str, ...] = _ordered_unique([s.village for s in self.storms])
        self.kinds: tuple[str, ...] = _ordered_unique([g.kind for g in self.groups.values()])
        self.risks: tuple[str, ...] = _ordered_unique([s.risk for s in self.storms])

    def season_index(self, season: str) -> int:
        return self.seasons.index(season)


def validate(ledger: Ledger) -> list[str]:
    """复算前的语义校验，返回问题清单（空表示通过）。"""
    issues: list[str] = []
    for group in ledger.groups.values():
        if not group.candidates:
            issues.append(f"{group.name} 没有候选归属村")
        unknown = [v for v in group.candidates if v not in ledger.villages]
        if unknown:
            issues.append(f"{group.name} 的候选村 {unknown} 不在降雨清单的分区中")
        if group.low < 0 or group.high < group.low:
            issues.append(f"{group.name} 的人口区间 [{group.low}, {group.high}] 不可用")
        if group.joined not in ledger.seasons:
            issues.append(f"{group.name} 的生效季 {group.joined} 不在季顺序中")
        if group.stop is not None and group.stop not in ledger.seasons:
            issues.append(f"{group.name} 的停用季 {group.stop} 不在季顺序中")
        if group.stop is not None and group.stop in ledger.seasons and group.joined in ledger.seasons:
            if ledger.season_index(group.stop) < ledger.season_index(group.joined):
                issues.append(f"{group.name} 的停用季早于生效季")
    for name, weights in ledger.standards.items():
        total = sum(weights.values())
        if abs(total - 1.0) > 1e-9:
            issues.append(f"标准人口 {name} 的权重合计为 {total}，未归一")
        for (kind, risk), weight in weights.items():
            if weight <= 0:
                issues.append(f"标准人口 {name} 的 {kind}/{risk} 权重必须为正")
        missing = [(k, r) for k in ledger.kinds for r in ledger.risks if (k, r) not in weights]
        if missing:
            issues.append(f"标准人口 {name} 缺少分层 {missing}")
    storm_keys = {(s.season, s.storm, s.village) for s in ledger.storms}
    for reach in ledger.reaches:
        season = next((s.season for s in ledger.storms if s.storm == reach.storm), None)
        if season is not None and (season, reach.storm, reach.village) not in storm_keys:
            issues.append(f"reach 中 {reach.storm}/{reach.village} 没有对应的降雨过程记录")
        if reach.state not in REACH_STATES:
            issues.append(f"reach 中 {reach.storm}/{reach.village} 的状态 {reach.state} 不可识别")
        if reach.state == "unknown" and reach.count is not None:
            issues.append(f"reach 中 {reach.storm}/{reach.village} 状态未知却记了人数")
        for option in reach.group_options:
            if option not in ledger.groups:
                issues.append(f"reach 中 {reach.storm}/{reach.village} 的候选群组 {option} 不存在")
    for storm in ledger.storms:
        if storm.state not in WEATHER_STATES:
            issues.append(f"{storm.storm}/{storm.village} 的过程状态 {storm.state} 不可识别")
        if storm.deadline_min <= 0:
            issues.append(f"{storm.storm}/{storm.village} 的送达时限必须为正")
    seen_channels: set[tuple[str, str]] = set()
    for row in ledger.channels:
        if (row.season, row.village) in seen_channels:
            issues.append(f"{row.season}/{row.village} 的渠道记录重复")
        seen_channels.add((row.season, row.village))
        if row.season not in ledger.seasons or row.village not in ledger.villages:
            issues.append(f"渠道记录 {row.season}/{row.village} 不在已知的季与分区中")
    for row in ledger.budgets:
        if row.season not in ledger.seasons or row.village not in ledger.villages:
            issues.append(f"额度记录 {row.season}/{row.village} 不在已知的季与分区中")
        if not 0.0 <= row.lowest <= 1.0:
            issues.append(f"额度记录 {row.season}/{row.village} 的最低覆盖水平必须在 [0, 1]")
        if row.locked < 0 or row.total < row.locked:
            issues.append(f"额度记录 {row.season}/{row.village} 的已承诺保障超出总额度")
        if row.share_limit < 0:
            issues.append(f"额度记录 {row.season}/{row.village} 的共同服务点额度不能为负")
    for appeal in ledger.appeals:
        group = ledger.groups.get(appeal.group)
        if group is None:
            issues.append(f"申诉 {appeal.ident} 指向不存在的群组 {appeal.group}")
            continue
        if appeal.village not in group.candidates:
            issues.append(
                f"申诉 {appeal.ident} 把 {appeal.group} 归到 {appeal.village}，"
                f"不在其候选村 {list(group.candidates)} 中"
            )
        if appeal.reported_season not in ledger.seasons:
            issues.append(f"申诉 {appeal.ident} 的申报季 {appeal.reported_season} 不在季顺序中")
    return issues


def load(location: str | Path) -> tuple[Ledger, list[str]]:
    """载入台账并做语义校验，返回 (台账, 问题清单)。"""
    ledger = Ledger(location)
    return ledger, validate(ledger)
