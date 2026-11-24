"""复算核心：归属口径、分层实测、标准化水准、差距与趋势。

口径约定：

- 及时率的点估计只使用可信记录（按时/超时）；状态未知的记录不进入
  点估计，但进入区间：全部按超时计为下界，全部按按时计为上界。
- 无危险过程的分层不进入任何比较，也不视为服务失败；有危险过程但
  没有可信触达记录的分层按 [0, 1] 区间处理并单独标识。
- 全县汇总只在单一归属情景内计算；跨情景只给敏感范围，不混合。
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

from .ledger import Ledger

STATUS_LABELS = {
    "no_danger": "无危险过程",
    "unmeasured": "有危险但无可信触达记录",
    "partial": "部分可信",
    "full": "证据完整",
}


@dataclass(frozen=True)
class Estimate:
    """一个指标的估计：点估计（可为 None）与区间下上界。"""

    point: float | None
    lo: float
    hi: float


@dataclass(frozen=True)
class Stratum:
    """一个 季×分区×风险 分层的实测汇总。"""

    season: str
    village: str
    risk: str
    confirmed: int
    within: int
    late: int
    unknown: int
    missing: int  # 确认危险过程但没有任何触达记录
    deadlines: tuple[int, ...]

    @property
    def credible(self) -> int:
        return self.within + self.late

    @property
    def status(self) -> str:
        if self.confirmed == 0:
            return "no_danger"
        if self.credible == 0:
            return "unmeasured"
        return "partial" if self.credible < self.confirmed else "full"

    @property
    def point(self) -> float | None:
        return None if self.credible == 0 else self.within / self.credible

    @property
    def lo(self) -> float | None:
        return None if self.confirmed == 0 else self.within / self.confirmed

    @property
    def hi(self) -> float | None:
        return None if self.confirmed == 0 else (
            (self.within + self.unknown + self.missing) / self.confirmed
        )

    def estimate(self) -> Estimate | None:
        """无危险过程的分层不参与加权，返回 None。"""
        if self.status == "no_danger":
            return None
        if self.status == "unmeasured":
            return Estimate(None, 0.0, 1.0)
        return Estimate(self.point, self.lo, self.hi)


def weighted_mean(parts: list[tuple[Estimate, float]]) -> Estimate | None:
    """按权重合成估计；权重合计为 0 时返回 None。"""
    usable = [(est, weight) for est, weight in parts if weight > 0]
    total = sum(weight for _, weight in usable)
    if not usable or total <= 0:
        return None
    lo = sum(est.lo * weight for est, weight in usable) / total
    hi = sum(est.hi * weight for est, weight in usable) / total
    if any(est.point is None for est, _ in usable):
        point = None
    else:
        point = sum(est.point * weight for est, weight in usable) / total
    return Estimate(point, lo, hi)


def build_strata(ledger: Ledger) -> dict[tuple[str, str, str], Stratum]:
    """把降雨清单与触达记录汇总成 季×分区×风险 分层。"""
    confirmed: dict[tuple[str, str, str], int] = {}
    deadlines: dict[tuple[str, str, str], set[int]] = {}
    for storm in ledger.storms:
        if storm.state != "confirmed":
            continue
        key = (storm.season, storm.village, storm.risk)
        confirmed[key] = confirmed.get(key, 0) + 1
        deadlines.setdefault(key, set()).add(storm.deadline_min)
    counts: dict[tuple[str, str, str], dict[str, int]] = {}
    storm_season = {storm.storm: storm.season for storm in ledger.storms}
    storm_state = {(storm.storm, storm.village): storm.state for storm in ledger.storms}
    for reach in ledger.reaches:
        season = storm_season.get(reach.storm)
        if season is None or storm_state.get((reach.storm, reach.village)) != "confirmed":
            continue
        risk = next(
            storm.risk
            for storm in ledger.storms
            if storm.storm == reach.storm and storm.village == reach.village
        )
        key = (season, reach.village, risk)
        counts.setdefault(key, {"within": 0, "late": 0, "unknown": 0})
        counts[key][reach.state] += 1
    strata: dict[tuple[str, str, str], Stratum] = {}
    for season in ledger.seasons:
        for village in ledger.villages:
            for risk in ledger.risks:
                key = (season, village, risk)
                tally = counts.get(key, {"within": 0, "late": 0, "unknown": 0})
                total_confirmed = confirmed.get(key, 0)
                recorded = tally["within"] + tally["late"] + tally["unknown"]
                strata[key] = Stratum(
                    season=season,
                    village=village,
                    risk=risk,
                    confirmed=total_confirmed,
                    within=tally["within"],
                    late=tally["late"],
                    unknown=tally["unknown"],
                    missing=max(0, total_confirmed - recorded),
                    deadlines=tuple(sorted(deadlines.get(key, set()))),
                )
    return strata


# ---------------------------------------------------------------- 归属与范围


def scope_groups(ledger: Ledger, season: str):
    """指标范围内的群组：生效季已到即进入；接收点停用但责任未变时不剔除。"""
    index = ledger.season_index(season)
    return [g for g in ledger.groups.values() if ledger.season_index(g.joined) <= index]


def is_stopped(ledger: Ledger, group, season: str) -> bool:
    return group.stop is not None and ledger.season_index(group.stop) <= ledger.season_index(
        season
    )


def appeal_fixed_groups(ledger: Ledger, season: str) -> set[str]:
    """在该季被申诉事实锁定归属的群组。"""
    index = ledger.season_index(season)
    fixed: set[str] = set()
    for appeal in ledger.appeals:
        reported = ledger.season_index(appeal.reported_season)
        if appeal.only_new_season and index < reported:
            continue
        fixed.add(appeal.group)
    return fixed


def resolve_attribution(ledger: Ledger, use_appeals: bool) -> dict[str, dict[str, str]]:
    """每个季一张归属表：基线取候选首项，申诉事实在其适用季覆盖基线。"""
    tables = {season: {g.name: g.candidates[0] for g in ledger.groups.values()}
              for season in ledger.seasons}
    if use_appeals:
        for appeal in ledger.appeals:
            reported = ledger.season_index(appeal.reported_season)
            for i, season in enumerate(ledger.seasons):
                if appeal.only_new_season and i < reported:
                    continue
                tables[season][appeal.group] = appeal.village
    return tables


def attribution_scenarios(ledger: Ledger, use_appeals: bool, season: str):
    """该季所有与台账一致的归属情景（仅枚举申诉未锁定的多候选群组）。"""
    base = resolve_attribution(ledger, use_appeals)[season]
    fixed = appeal_fixed_groups(ledger, season) if use_appeals else set()
    ambiguous = [
        g
        for g in scope_groups(ledger, season)
        if len(g.candidates) > 1 and g.name not in fixed
    ]
    scenarios = []
    for combo in product(*(g.candidates for g in ambiguous)):
        scenario = dict(base)
        for group, village in zip(ambiguous, combo):
            scenario[group.name] = village
        scenarios.append(scenario)
    return scenarios


def village_population(
    ledger: Ledger,
    attribution: dict[str, str],
    season: str,
    village: str,
    kind: str | None = None,
) -> tuple[int, int]:
    """分区在指标范围内的人口区间（low 为可证明的共同部分）。"""
    low = high = 0
    for group in scope_groups(ledger, season):
        if attribution[group.name] != village:
            continue
        if kind is not None and group.kind != kind:
            continue
        low += group.low
        high += group.high
    return low, high


def county_population(ledger: Ledger, attribution: dict[str, str], season: str) -> tuple[int, int]:
    low = high = 0
    for group in scope_groups(ledger, season):
        low += group.low
        high += group.high
    return low, high


# ---------------------------------------------------------------- 水准计算


def village_level(
    ledger: Ledger,
    strata: dict,
    season: str,
    village: str,
    standard: dict[tuple[str, str], float],
    kind: str | None = None,
) -> Estimate | None:
    """分区标准化及时水准；kind 为 None 时按标准人口的类群权重合并。"""
    parts = []
    for risk in ledger.risks:
        estimate = strata[(season, village, risk)].estimate()
        if estimate is None:
            continue
        if kind is None:
            weight = sum(standard[(k, risk)] for k in ledger.kinds)
        else:
            weight = standard[(kind, risk)]
        parts.append((estimate, weight))
    return weighted_mean(parts)


def village_floor(ledger: Ledger, strata: dict, season: str, village: str) -> Estimate | None:
    """最低服务水准：各风险分层及时率的最小值。"""
    estimates = [
        strata[(season, village, risk)].estimate()
        for risk in ledger.risks
        if strata[(season, village, risk)].estimate() is not None
    ]
    if not estimates:
        return None
    points = [est.point for est in estimates]
    return Estimate(
        None if any(p is None for p in points) else min(points),
        min(est.lo for est in estimates),
        min(est.hi for est in estimates),
    )


def floor_comparable(ledger: Ledger, strata: dict, season: str, village: str) -> bool:
    """该分区的确认风险结构是否覆盖全部风险层（才能进入最低水准对照）。"""
    return all(
        strata[(season, village, risk)].confirmed > 0 for risk in ledger.risks
    )


def county_level(
    ledger: Ledger,
    strata: dict,
    attribution: dict[str, str],
    season: str,
    standard: dict[tuple[str, str], float],
    pop_basis: str = "mid",
    kind: str | None = None,
) -> Estimate | None:
    """全县标准化水准：先按层内人口加权村率，再按标准人口合成。

    pop_basis 取 low（可证明的共同部分，严格口径）、high 或 mid。
    """
    kinds = [kind] if kind is not None else list(ledger.kinds)
    parts = []
    for k in kinds:
        for risk in ledger.risks:
            entries = []
            for village in ledger.villages:
                estimate = strata[(season, village, risk)].estimate()
                if estimate is None:
                    continue
                low, high = village_population(ledger, attribution, season, village, k)
                if pop_basis == "low":
                    pop = low
                elif pop_basis == "high":
                    pop = high
                else:
                    pop = (low + high) / 2.0
                if pop <= 0:
                    continue
                entries.append((estimate, float(pop)))
            rate = weighted_mean(entries)
            if rate is None:
                continue
            parts.append((rate, standard[(k, risk)]))
    return weighted_mean(parts)


def county_level_interval(
    ledger: Ledger,
    strata: dict,
    attribution: dict[str, str],
    season: str,
    standard: dict[tuple[str, str], float],
    kind: str | None = None,
) -> Estimate | None:
    """全县水准的合并口径：点估计用 mid 人口，区间覆盖 low/high 两种人口解释。"""
    mid = county_level(ledger, strata, attribution, season, standard, "mid", kind)
    low = county_level(ledger, strata, attribution, season, standard, "low", kind)
    high = county_level(ledger, strata, attribution, season, standard, "high", kind)
    if mid is None or low is None or high is None:
        return None
    return Estimate(
        mid.point if low.point is not None and high.point is not None else None,
        min(low.lo, high.lo),
        max(low.hi, high.hi),
    )


# ---------------------------------------------------------------- 差距与趋势


def village_gap(ledger: Ledger, strata: dict, season: str, standard_name: str):
    """分区差距：同一标准人口下各区标准化水准的极差与稳健最差分区。"""
    standard = ledger.standards[standard_name]
    levels = {
        village: village_level(ledger, strata, season, village, standard)
        for village in ledger.villages
    }
    points = {v: est.point for v, est in levels.items() if est is not None and est.point is not None}
    robust_worst = [
        v
        for v, est in levels.items()
        if est is not None
        and all(
            other is not None and est.hi <= other.lo
            for w, other in levels.items()
            if w != v
        )
    ]
    result = {
        "levels": levels,
        "robust_worst": robust_worst,
        "point_gap": None,
        "best": None,
        "worst": None,
    }
    if len(points) == len(ledger.villages) and points:
        best = max(points, key=points.get)
        worst = min(points, key=points.get)
        result.update(
            point_gap=points[best] - points[worst], best=best, worst=worst
        )
    return result


def kind_gap(
    ledger: Ledger,
    strata: dict,
    attribution: dict[str, str],
    season: str,
    standard_name: str,
):
    """多类受众差距：全县口径下各类受众标准化水准之差。"""
    standard = ledger.standards[standard_name]
    levels = {
        kind: county_level_interval(ledger, strata, attribution, season, standard, kind=kind)
        for kind in ledger.kinds
    }
    gaps = {}
    kinds = list(ledger.kinds)
    for i, first in enumerate(kinds):
        for second in kinds[i + 1 :]:
            a, b = levels[first], levels[second]
            if a is None or b is None:
                gaps[f"{first}|{second}"] = None
                continue
            point = None if a.point is None or b.point is None else a.point - b.point
            gaps[f"{first}|{second}"] = Estimate(point, a.lo - b.hi, a.hi - b.lo)
    return {"levels": levels, "gaps": gaps}


def trend_verdict(before: Estimate | None, after: Estimate | None) -> str:
    """单套权重下的趋势判定：区间分离才算稳健。"""
    if before is None or after is None:
        return "证据不足"
    if after.lo > before.hi:
        return "稳健改善"
    if after.hi < before.lo:
        return "稳健退步"
    if before.point is not None and after.point is not None:
        if after.point > before.point:
            return "点估计改善（非稳健）"
        if after.point < before.point:
            return "点估计退步（非稳健）"
        return "点估计持平"
    return "证据不足"


def combine_verdicts(verdicts: list[str]) -> str:
    """跨多套参考权重的合成：全部一致才算稳健，否则只作局部观察。"""
    if not verdicts or any(v == "证据不足" for v in verdicts):
        return "证据不足（局部观察）"
    if all(v == "稳健改善" for v in verdicts):
        return "稳健改善"
    if all(v == "稳健退步" for v in verdicts):
        return "稳健退步"
    ups = {"稳健改善", "点估计改善（非稳健）"}
    downs = {"稳健退步", "点估计退步（非稳健）"}
    if all(v in ups for v in verdicts):
        return "方向一致改善（局部观察）"
    if all(v in downs for v in verdicts):
        return "方向一致退步（局部观察）"
    if all(v == "点估计持平" for v in verdicts):
        return "点估计持平（非稳健）"
    return "方向不一致（局部观察）"


def co_changes(
    ledger: Ledger,
    attribution_before: dict[str, str],
    attribution_after: dict[str, str],
    season_before: str,
    season_after: str,
    village: str,
) -> list[str]:
    """两季之间与该分区同时发生的变化，供解释趋势时对照。"""
    changes: list[str] = []
    channel_before = next(
        (c for c in ledger.channels if c.season == season_before and c.village == village), None
    )
    channel_after = next(
        (c for c in ledger.channels if c.season == season_after and c.village == village), None
    )
    if channel_before and channel_after:
        if channel_before.channel != channel_after.channel:
            changes.append(
                f"服务渠道替换 {channel_before.channel}→{channel_after.channel}"
            )
        if channel_before.common_point != channel_after.common_point:
            changes.append(
                f"共同接收点变更 {channel_before.common_point}→{channel_after.common_point}"
            )
    for risk in ledger.risks:
        key_b = sum(
            1
            for s in ledger.storms
            if s.season == season_before
            and s.village == village
            and s.risk == risk
            and s.state == "confirmed"
        )
        key_a = sum(
            1
            for s in ledger.storms
            if s.season == season_after
            and s.village == village
            and s.risk == risk
            and s.state == "confirmed"
        )
        if key_b != key_a:
            changes.append(f"{risk} 风险过程数 {key_b}→{key_a}")
    before = {g.name for g in scope_groups(ledger, season_before)
              if attribution_before[g.name] == village}
    after = {g.name for g in scope_groups(ledger, season_after)
             if attribution_after[g.name] == village}
    for name in sorted(after - before):
        changes.append(f"群组 {name} 进入本分区指标范围")
    for name in sorted(before - after):
        changes.append(f"群组 {name} 离开本分区指标范围")
    for name in sorted(before & after):
        group = ledger.groups[name]
        if is_stopped(ledger, group, season_after) and not is_stopped(
            ledger, group, season_before
        ):
            changes.append(f"群组 {name} 的接收点季中停用（责任 {group.responsibility} 未变，仍计入）")
    return changes


# ---------------------------------------------------------------- 实际覆盖


def actual_coverage(ledger: Ledger, season: str) -> dict[str, dict]:
    """实际当季覆盖结果：观测计数原样保留，跨区合计按证据去重。"""
    storm_state = {(s.storm, s.village): s.state for s in ledger.storms}
    storm_season = {s.storm: s.season for s in ledger.storms}
    per_village: dict[str, dict] = {
        village: {"within": 0, "late": 0, "unknown": 0, "ontime_count": 0}
        for village in ledger.villages
    }
    proofs: dict[str, int] = {}
    for reach in ledger.reaches:
        if storm_season.get(reach.storm) != season:
            continue
        if storm_state.get((reach.storm, reach.village)) != "confirmed":
            continue
        entry = per_village[reach.village]
        entry[reach.state] += 1
        if reach.state == "within" and reach.count is not None:
            entry["ontime_count"] += reach.count
            proofs[reach.proof] = max(proofs.get(reach.proof, 0), reach.count)
    for village, entry in per_village.items():
        credible = entry["within"] + entry["late"]
        entry["observed_rate"] = None if credible == 0 else round(entry["within"] / credible, 4)
    return {
        "villages": per_village,
        "proof_dedup_ontime_count": sum(proofs.values()),
        "proof_note": "相同 proof 为同一份证据，跨分区人数不可直接累加；此处按 proof 取最大值后合计",
    }
