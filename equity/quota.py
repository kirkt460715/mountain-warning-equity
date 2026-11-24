"""改进额度复算。

规则：

- 已承诺的应急信息保障（locked）优先保留，新增额度最多为 total-locked，
  且投向共同服务点的部分不超过 share_limit；
- 缺口分两层：区间上限仍低于最低覆盖水平的部分为"确有缺口"（两套
  参考人口都支持，取较小者），区间下界低于最低覆盖水平的部分为
  "可能缺口"（任一标准支持，取较大者）；
- 新增核准额度只覆盖确有缺口；可能缺口列为待补证额度，证据补齐前
  不核准；
- 短缺按去重后的归属人口计算：同一网格内按类群或渠道切片的短缺
  可能对应同一批人，各图表的下降幅度不可直接相加；
- 申诉重算时，证据不完整（存在未知记录）的分区核准额度只增不减，
  不给缺证群组扣额度。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .analysis import (
    Estimate,
    build_strata,
    resolve_attribution,
    scope_groups,
    village_level,
    village_population,
)
from .ledger import Ledger


@dataclass
class VillageAllocation:
    village: str
    lowest: float
    locked: float
    total: float
    share_limit: float
    population: tuple[int, int]
    definite_deficit: float  # 确有缺口（两套标准人口都支持）
    possible_deficit: float  # 可能缺口（任一标准支持）
    need_certain: float  # 确有缺口 × 人口下界
    need_possible: float  # 可能缺口 × 人口上界
    approved_new: float  # 本轮可核准的新增额度
    deferred: float  # 待补证后可核准的上限
    unmet: float  # 确有缺口中超出本轮额度的部分
    naive_slice_sum: float  # 按类群切片下降幅度的合计（不可直接核准）
    evidence_complete: bool
    basis: list[str] = field(default_factory=list)


def _deficits(levels: dict[str, Estimate | None], lowest: float) -> tuple[float, float]:
    definite_parts = []
    possible_parts = []
    for est in levels.values():
        if est is None:
            continue
        definite_parts.append(max(0.0, lowest - est.hi))
        possible_parts.append(max(0.0, lowest - est.lo))
    if not definite_parts:
        return 0.0, 0.0
    return min(definite_parts), max(possible_parts)


def allocate_village(
    ledger: Ledger,
    strata: dict,
    attribution: dict[str, str],
    season: str,
    budget,
) -> VillageAllocation:
    levels = {
        name: village_level(ledger, strata, season, budget.village, weights)
        for name, weights in ledger.standards.items()
    }
    definite, possible = _deficits(levels, budget.lowest)
    pop_low, pop_high = village_population(ledger, attribution, season, budget.village)
    need_certain = definite * pop_low
    need_possible = possible * pop_high
    cap_new = max(0.0, budget.total - budget.locked)
    point_cap = min(cap_new, budget.share_limit)
    approved = min(need_certain, point_cap)
    deferred = max(0.0, min(need_possible, point_cap) - approved)
    unmet = max(0.0, need_certain - approved)

    naive_sum = 0.0
    for kind in ledger.kinds:
        kind_levels = {
            name: village_level(ledger, strata, season, budget.village, weights, kind=kind)
            for name, weights in ledger.standards.items()
        }
        _, kind_possible = _deficits(kind_levels, budget.lowest)
        _, kind_high = village_population(ledger, attribution, season, budget.village, kind)
        naive_sum += kind_possible * kind_high

    evidence_complete = all(
        strata[(season, budget.village, risk)].status == "full" for risk in ledger.risks
    )
    basis = [
        f"最低覆盖水平 {budget.lowest}，两套标准人口下的确有缺口 {definite:.4f}、可能缺口 {possible:.4f}",
        f"归属人口区间 [{pop_low}, {pop_high}]（low 为可证明的共同部分）",
        f"新增额度上限 min(total-locked={cap_new:g}, share_limit={budget.share_limit:g}) = {point_cap:g}",
        "按类群切片的短缺合计不可直接核准：同一切片可能对应同一批人",
    ]
    return VillageAllocation(
        village=budget.village,
        lowest=budget.lowest,
        locked=budget.locked,
        total=budget.total,
        share_limit=budget.share_limit,
        population=(pop_low, pop_high),
        definite_deficit=definite,
        possible_deficit=possible,
        need_certain=need_certain,
        need_possible=need_possible,
        approved_new=approved,
        deferred=deferred,
        unmet=unmet,
        naive_slice_sum=naive_sum,
        evidence_complete=evidence_complete,
        basis=basis,
    )


def _apply_shared_point_caps(ledger: Ledger, season: str, allocations: dict[str, VillageAllocation]):
    """多个分区共用同一接收点时，合计核准不超过共享方 share_limit 的最小值。"""
    points: dict[str, list[str]] = {}
    for row in ledger.channels:
        if row.season == season and row.village in allocations:
            points.setdefault(row.common_point, []).append(row.village)
    adjustments = []
    for point, villages in sorted(points.items()):
        if len(villages) < 2:
            continue
        cap = min(allocations[v].share_limit for v in villages)
        total = sum(allocations[v].approved_new for v in villages)
        if total > cap and total > 0:
            scale = cap / total
            for v in villages:
                before = allocations[v].approved_new
                allocations[v].approved_new = before * scale
                allocations[v].deferred = max(
                    0.0, allocations[v].deferred - (before - allocations[v].approved_new)
                )
            adjustments.append(
                f"共同接收点 {point} 由 {sorted(villages)} 共用，合计核准 {total:.4f} 超出共享上限 {cap:g}，已按比例压减"
            )
    return adjustments


def allocate(
    ledger: Ledger, season: str, use_appeals: bool
) -> tuple[dict[str, VillageAllocation], list[str], dict[str, str]]:
    """一个归属情景下的额度复算，返回 (分区额度, 共享点调整说明, 归属表)。"""
    strata = build_strata(ledger)
    attribution = resolve_attribution(ledger, use_appeals)[season]
    budgets = [b for b in ledger.budgets if b.season == season]
    allocations = {
        b.village: allocate_village(ledger, strata, attribution, season, b) for b in budgets
    }
    adjustments = _apply_shared_point_caps(ledger, season, allocations)
    return allocations, adjustments, attribution


def compare_appeals(ledger: Ledger, season: str) -> dict:
    """申诉重算对照：基线 vs 计入申诉，缺证分区核准额度只增不减。"""
    before, adj_before, attr_before = allocate(ledger, season, use_appeals=False)
    after, adj_after, attr_after = allocate(ledger, season, use_appeals=True)

    protections = []
    for village, alloc in after.items():
        base = before[village].approved_new
        if not alloc.evidence_complete and alloc.approved_new < base:
            protections.append(
                f"{village} 证据不完整（存在未知触达记录），申诉情景下核准额度 "
                f"{alloc.approved_new:.4f} 低于基线 {base:.4f}，按缺证不扣额度保留基线"
            )
            alloc.approved_new = base

    moved = []
    for group in ledger.groups.values():
        if attr_before.get(group.name) != attr_after.get(group.name):
            moved.append(
                {
                    "group": group.name,
                    "from": attr_before.get(group.name),
                    "to": attr_after.get(group.name),
                }
            )
    affected_villages = sorted(
        {
            v
            for v in after
            if abs(after[v].approved_new - before[v].approved_new) > 1e-12
            or after[v].population != before[v].population
        }
    )
    affected_groups = sorted(
        {
            g.name
            for g in scope_groups(ledger, season)
            if attr_after.get(g.name) in affected_villages
            or attr_before.get(g.name) in affected_villages
        }
    )
    return {
        "before": before,
        "after": after,
        "adjustments_before": adj_before,
        "adjustments_after": adj_after,
        "protections": protections,
        "moved_groups": moved,
        "affected_villages": affected_villages,
        "affected_groups": affected_groups,
        "locked_note": "已承诺保障 locked 在两个情景中原样保留",
    }
