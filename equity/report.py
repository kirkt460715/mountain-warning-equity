"""把复算结果组装成可核对的报告（JSON 友好的字典结构）。"""

from __future__ import annotations

from .analysis import (
    STATUS_LABELS,
    Estimate,
    actual_coverage,
    attribution_scenarios,
    build_strata,
    co_changes,
    combine_verdicts,
    county_level,
    county_level_interval,
    county_population,
    floor_comparable,
    is_stopped,
    kind_gap,
    resolve_attribution,
    scope_groups,
    trend_verdict,
    village_floor,
    village_gap,
    village_level,
    village_population,
)
from .ledger import Ledger
from .quota import allocate, compare_appeals


def _num(value: float | None, digits: int = 4):
    return None if value is None else round(value, digits)


def _est(est: Estimate | None) -> dict | None:
    if est is None:
        return None
    return {"点估计": _num(est.point), "区间": [_num(est.lo), _num(est.hi)]}


def _stratum_view(stratum) -> dict:
    return {
        "确认危险过程": stratum.confirmed,
        "按时": stratum.within,
        "超时": stratum.late,
        "未知": stratum.unknown,
        "缺记录": stratum.missing,
        "状态": STATUS_LABELS[stratum.status],
        "规定时限分钟": list(stratum.deadlines),
        "及时率点估计": _num(stratum.point),
        "及时率区间": [_num(stratum.lo), _num(stratum.hi)],
    }


def season_report(ledger: Ledger, season: str, use_appeals: bool) -> dict:
    strata = build_strata(ledger)
    attribution_all = resolve_attribution(ledger, use_appeals)
    attribution = attribution_all[season]
    index = ledger.season_index(season)

    risk_structure = {}
    strata_view = {}
    for village in ledger.villages:
        per_risk = {}
        no_event = sorted(
            s.storm
            for s in ledger.storms
            if s.season == season and s.village == village and s.state == "no_event"
        )
        for risk in ledger.risks:
            stratum = strata[(season, village, risk)]
            per_risk[risk] = stratum.confirmed
        risk_structure[village] = {"确认过程": per_risk, "无过程清单": no_event}
        strata_view[village] = {
            risk: _stratum_view(strata[(season, village, risk)]) for risk in ledger.risks
        }

    village_levels = {}
    for village in ledger.villages:
        by_standard = {
            name: _est(village_level(ledger, strata, season, village, weights))
            for name, weights in ledger.standards.items()
        }
        by_kind = {
            kind: {
                name: _est(village_level(ledger, strata, season, village, weights, kind=kind))
                for name, weights in ledger.standards.items()
            }
            for kind in ledger.kinds
        }
        village_levels[village] = {
            "标准化水准": by_standard,
            "分类受众水准": by_kind,
            "最低服务水准": _est(village_floor(ledger, strata, season, village)),
            "最低水准可对照": floor_comparable(ledger, strata, season, village),
            "归属人口区间": list(village_population(ledger, attribution, season, village)),
        }

    group_levels = {}
    for group in scope_groups(ledger, season):
        village = attribution[group.name]
        group_levels[group.name] = {
            "归属": village,
            "受众类": group.kind,
            "人口区间": [group.low, group.high],
            "接收点已停用": is_stopped(ledger, group, season),
            "标准化水准": {
                name: _est(
                    village_level(ledger, strata, season, village, weights, kind=group.kind)
                )
                for name, weights in ledger.standards.items()
            },
            "最低服务水准": _est(village_floor(ledger, strata, season, village)),
        }

    gaps = {}
    for name in ledger.standards:
        gap = village_gap(ledger, strata, season, name)
        gaps[name] = {
            "点估计差距": _num(gap["point_gap"]),
            "最好分区": gap["best"],
            "最差分区": gap["worst"],
            "稳健最差分区": gap["robust_worst"],
        }
    audience = {}
    for name in ledger.standards:
        audience_gap = kind_gap(ledger, strata, attribution, season, name)
        audience[name] = {
            "分类水准": {k: _est(v) for k, v in audience_gap["levels"].items()},
            "类群差距": {pair: _est(est) for pair, est in audience_gap["gaps"].items()},
        }

    county = {}
    scenarios = attribution_scenarios(ledger, use_appeals, season)
    for name, weights in ledger.standards.items():
        chosen = county_level_interval(ledger, strata, attribution, season, weights)
        scenario_points = []
        for scenario in scenarios:
            est = county_level(ledger, strata, scenario, season, weights, "mid")
            if est is not None and est.point is not None:
                scenario_points.append(est.point)
        county[name] = {
            "本情景": _est(chosen),
            "归属敏感范围": (
                [_num(min(scenario_points)), _num(max(scenario_points))]
                if scenario_points
                else None
            ),
            "敏感范围说明": "全部一致归属情景下点估计的最小/最大；区间口径见本情景",
        }
    pop_low, pop_high = county_population(ledger, attribution, season)

    trends = {}
    if index > 0:
        previous = ledger.seasons[index - 1]
        attr_prev = attribution_all[previous]
        for village in ledger.villages:
            verdicts = []
            per_standard = {}
            for name, weights in ledger.standards.items():
                before = village_level(ledger, strata, previous, village, weights)
                after = village_level(ledger, strata, season, village, weights)
                verdict = trend_verdict(before, after)
                verdicts.append(verdict)
                per_standard[name] = {
                    "上季": _est(before),
                    "本季": _est(after),
                    "判定": verdict,
                }
            trends[village] = {
                "对比季": previous,
                "分标准": per_standard,
                "综合判定": combine_verdicts(verdicts),
                "同期变化": co_changes(ledger, attr_prev, attribution, previous, season, village),
            }
        county_verdicts = []
        county_trend = {}
        for name, weights in ledger.standards.items():
            before = county_level_interval(ledger, strata, attr_prev, previous, weights)
            after = county_level_interval(ledger, strata, attribution, season, weights)
            verdict = trend_verdict(before, after)
            county_verdicts.append(verdict)
            county_trend[name] = {"上季": _est(before), "本季": _est(after), "判定": verdict}
        trends["全县"] = {
            "对比季": previous,
            "分标准": county_trend,
            "综合判定": combine_verdicts(county_verdicts),
        }

    special = {"无危险过程": [], "有危险无可信记录": [], "季中停用仍计入": []}
    for village in ledger.villages:
        for risk in ledger.risks:
            stratum = strata[(season, village, risk)]
            if stratum.status == "no_danger":
                special["无危险过程"].append(
                    {"分区": village, "风险层": risk, "后续动作": "不进入比较，无需动作"}
                )
            elif stratum.status == "unmeasured":
                special["有危险无可信记录"].append(
                    {
                        "分区": village,
                        "风险层": risk,
                        "确认过程": stratum.confirmed,
                        "后续动作": "补建触达证据；结论与额度只按区间处理",
                    }
                )
    for group in scope_groups(ledger, season):
        if is_stopped(ledger, group, season):
            special["季中停用仍计入"].append(
                {
                    "群组": group.name,
                    "归属": attribution[group.name],
                    "服务责任": group.responsibility,
                    "说明": "接收点停用但责任未变，继续计入指标范围，不剔除人群",
                }
            )

    support = []
    for village in ledger.villages:
        for risk in ledger.risks:
            stratum = strata[(season, village, risk)]
            if stratum.status == "partial":
                support.append(
                    f"{village}/{risk} 可信记录 {stratum.credible}/{stratum.confirmed}，"
                    f"只能给出区间 [{_num(stratum.lo)}, {_num(stratum.hi)}]"
                )
            elif stratum.status == "unmeasured":
                support.append(f"{village}/{risk} 无可信记录，只能给出区间 [0, 1]")

    return {
        "季": season,
        "归属情景": "计入申诉" if use_appeals else "基线（未计申诉）",
        "风险结构": risk_structure,
        "分层实测": strata_view,
        "村级水准": village_levels,
        "群组水准": group_levels,
        "分区差距": gaps,
        "受众差距": audience,
        "全县水准": county,
        "全县人口区间": [pop_low, pop_high],
        "趋势": trends,
        "实际覆盖": actual_coverage(ledger, season),
        "特别分组": special,
        "支撑说明": support,
    }


def full_report(ledger: Ledger, use_appeals: bool, season: str | None = None) -> dict:
    seasons = [season] if season else list(ledger.seasons)
    return {
        "口径说明": {
            "标准人口": {
                name: {f"{k}/{r}": w for (k, r), w in sorted(weights.items())}
                for name, weights in ledger.standards.items()
            },
            "严格比较人口口径": "low（固定户与流动聚居点可证明的共同部分）",
            "区间口径": "未知触达记录两端极端；人口解释取 low/high 两端",
            "稳健判定": "两套参考权重、全部一致归属情景与区间端点下结论均成立",
            "全县一致性": "全县汇总只在单一归属情景内计算，跨情景只给敏感范围",
        },
        "归属情景": "计入申诉" if use_appeals else "基线（未计申诉）",
        "季报": {s: season_report(ledger, s, use_appeals) for s in seasons},
    }


def _allocation_view(alloc) -> dict:
    return {
        "最低覆盖水平": alloc.lowest,
        "已承诺保障": alloc.locked,
        "总额度": alloc.total,
        "共享点上限": alloc.share_limit,
        "归属人口区间": list(alloc.population),
        "确有缺口": _num(alloc.definite_deficit),
        "可能缺口": _num(alloc.possible_deficit),
        "确有缺口折算额度": _num(alloc.need_certain),
        "可能缺口折算额度": _num(alloc.need_possible),
        "本轮核准新增": _num(alloc.approved_new),
        "待补证额度上限": _num(alloc.deferred),
        "确有缺口未满足": _num(alloc.unmet),
        "切片下降合计（不可直接核准）": _num(alloc.naive_slice_sum),
        "证据完整": alloc.evidence_complete,
        "依据": alloc.basis,
    }


def allocation_report(ledger: Ledger, season: str, use_appeals: bool) -> dict:
    allocations, adjustments, attribution = allocate(ledger, season, use_appeals)
    return {
        "季": season,
        "归属情景": "计入申诉" if use_appeals else "基线（未计申诉）",
        "归属表": {g: attribution[g] for g in sorted(attribution)},
        "共享点调整": adjustments,
        "分区额度": {v: _allocation_view(a) for v, a in allocations.items()},
        "口径说明": [
            "已承诺保障优先保留，新增额度只覆盖确有缺口",
            "可能缺口列为待补证额度，证据补齐前不核准",
            "同一网格跨类群/渠道的短缺可能对应同一批人，可核准额度按去重人口计算，不按各图表下降幅度相加",
        ],
    }


def appeal_report(ledger: Ledger, season: str) -> dict:
    result = compare_appeals(ledger, season)
    return {
        "季": season,
        "申诉清单": [
            {
                "编号": a.ident,
                "群组": a.group,
                "归属": a.village,
                "申报季": a.reported_season,
                "仅当季": a.only_new_season,
            }
            for a in ledger.appeals
        ],
        "归属变动": result["moved_groups"],
        "受影响分区": result["affected_villages"],
        "受影响群组": result["affected_groups"],
        "缺证保护": result["protections"],
        "保障说明": result["locked_note"],
        "共享点调整": {
            "基线": result["adjustments_before"],
            "计入申诉": result["adjustments_after"],
        },
        "额度对照": {
            village: {
                "基线核准": _num(result["before"][village].approved_new),
                "申诉后核准": _num(result["after"][village].approved_new),
                "基线待补证": _num(result["before"][village].deferred),
                "申诉后待补证": _num(result["after"][village].deferred),
                "已承诺保障": result["after"][village].locked,
            }
            for village in result["after"]
        },
    }


def query_report(
    ledger: Ledger, season: str, village: str | None = None, group: str | None = None
) -> dict:
    """经办人回查：指定季与分区（或群组）的指标适用范围、额度变动与依据。"""
    strata = build_strata(ledger)
    attr_base = resolve_attribution(ledger, False)[season]
    attr_appeal = resolve_attribution(ledger, True)[season]
    out: dict = {"季": season}

    if group is not None:
        g = ledger.groups[group]
        village = attr_appeal[group]
        out["群组"] = {
            "编号": g.name,
            "受众类": g.kind,
            "候选归属": list(g.candidates),
            "人口区间": [g.low, g.high],
            "生效季": g.joined,
            "停用季": g.stop,
            "服务责任": g.responsibility,
            "本季归属": {"基线": attr_base[group], "计入申诉": attr_appeal[group]},
            "在指标范围": g in scope_groups(ledger, season),
            "接收点已停用": is_stopped(ledger, g, season),
        }

    if village is None:
        return out

    in_scope = []
    for g in scope_groups(ledger, season):
        if attr_appeal[g.name] == village or attr_base[g.name] == village:
            in_scope.append(
                {
                    "群组": g.name,
                    "受众类": g.kind,
                    "人口区间": [g.low, g.high],
                    "基线归属": attr_base[g.name],
                    "申诉后归属": attr_appeal[g.name],
                    "接收点已停用": is_stopped(ledger, g, season),
                    "服务责任": g.responsibility,
                }
            )
    applicability = []
    for risk in ledger.risks:
        stratum = strata[(season, village, risk)]
        status = stratum.status
        if status == "full":
            applicability.append(f"{risk} 层证据完整，点估计与严格比较均适用")
        elif status == "partial":
            applicability.append(
                f"{risk} 层部分可信（{stratum.credible}/{stratum.confirmed}），"
                f"只适用区间 [{_num(stratum.lo)}, {_num(stratum.hi)}]"
            )
        elif status == "unmeasured":
            applicability.append(f"{risk} 层有危险但无可信记录，只适用区间 [0, 1]，需补证")
        else:
            applicability.append(f"{risk} 层无危险过程，不进入比较")
    if floor_comparable(ledger, strata, season, village):
        applicability.append("风险结构覆盖全部风险层，可参加最低服务水准对照")
    else:
        applicability.append("风险结构不完整，不参加最低服务水准对照")
    applicability.append("人均类严格比较只适用 low 人口口径（可证明的共同部分）")

    out["分区"] = village
    out["指标范围内群组"] = in_scope
    out["归属人口区间"] = {
        "基线": list(village_population(ledger, attr_base, season, village)),
        "计入申诉": list(village_population(ledger, attr_appeal, season, village)),
    }
    out["分层实测"] = {risk: _stratum_view(strata[(season, village, risk)]) for risk in ledger.risks}
    out["指标适用范围"] = applicability

    budgets = [b for b in ledger.budgets if b.season == season and b.village == village]
    if budgets:
        comparison = compare_appeals(ledger, season)
        before = comparison["before"][village]
        after = comparison["after"][village]
        out["额度变动"] = {
            "基线": _allocation_view(before),
            "计入申诉": _allocation_view(after),
            "缺证保护": [p for p in comparison["protections"] if village in p],
            "已承诺保障说明": comparison["locked_note"],
        }
    appeals = [
        {"编号": a.ident, "群组": a.group, "归属": a.village, "仅当季": a.only_new_season}
        for a in ledger.appeals
        if a.village == village
        or attr_base.get(a.group) == village
        or attr_appeal.get(a.group) == village
    ]
    out["相关申诉"] = appeals
    return out
