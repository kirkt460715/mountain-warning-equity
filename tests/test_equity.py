"""equity 复算工具的行为测试：范围、归属、区间、趋势、额度与申诉保护。"""

import csv
import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from shutil import copytree
from tempfile import TemporaryDirectory

from equity import analysis, ledger as ledger_mod, quota, report
from equity.__main__ import main

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def load():
    ledger, issues = ledger_mod.load(DATA)
    assert not issues, issues
    return ledger


class LedgerTests(unittest.TestCase):
    def test_semantic_validation_passes_on_supplied_data(self):
        ledger, issues = ledger_mod.load(DATA)
        self.assertEqual(issues, [])
        self.assertEqual(ledger.seasons, ("A", "B", "C"))
        self.assertEqual(ledger.villages, ("T1", "T2", "T3"))

    def _broken_copy(self, table, mutate):
        directory = TemporaryDirectory()
        target = Path(directory.name) / "data"
        copytree(DATA, target)
        file = target / f"{table}.csv"
        with file.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
            fields = rows[0].keys()
        mutate(rows)
        with file.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(fields))
            writer.writeheader()
            writer.writerows(rows)
        self.addCleanup(directory.cleanup)
        return target

    def test_appeal_outside_candidates_rejected(self):
        target = self._broken_copy("appeals", lambda rows: rows.__setitem__(0, {**rows[0], "village": "T3"}))
        # P2 的候选村为 T1|T2，申诉改到 T3 必须被拒绝
        _, issues = ledger_mod.load(target)
        self.assertTrue(any("候选村" in issue for issue in issues), issues)

    def test_standard_weights_must_be_normalized(self):
        def mutate(rows):
            rows[0]["weight"] = "0.9"

        target = self._broken_copy("standards", mutate)
        _, issues = ledger_mod.load(target)
        self.assertTrue(any("归一" in issue for issue in issues), issues)

    def test_population_interval_must_be_ordered(self):
        def mutate(rows):
            rows[0]["low"] = "95"

        target = self._broken_copy("people", mutate)
        _, issues = ledger_mod.load(target)
        self.assertTrue(any("人口区间" in issue for issue in issues), issues)


class ScopeAndAttributionTests(unittest.TestCase):
    def setUp(self):
        self.ledger = load()

    def test_joined_season_controls_scope(self):
        in_a = {g.name for g in analysis.scope_groups(self.ledger, "A")}
        self.assertEqual(in_a, {"P1", "P2", "P3", "P5"})
        in_c = {g.name for g in analysis.scope_groups(self.ledger, "C")}
        self.assertEqual(in_c, {"P1", "P2", "P3", "P4", "P5", "P6"})

    def test_stopped_points_stay_in_scope(self):
        # 季中停用的接收点不能直接把对应人群剔除
        in_c = {g.name for g in analysis.scope_groups(self.ledger, "C")}
        self.assertIn("P1", in_c)
        self.assertIn("P6", in_c)
        self.assertTrue(analysis.is_stopped(self.ledger, self.ledger.groups["P1"], "C"))
        self.assertFalse(analysis.is_stopped(self.ledger, self.ledger.groups["P1"], "B"))

    def test_baseline_attribution_uses_first_candidate(self):
        table = analysis.resolve_attribution(self.ledger, use_appeals=False)
        self.assertEqual(table["C"]["P2"], "T1")
        self.assertEqual(table["C"]["P4"], "T2")

    def test_appeals_retroactive_and_new_season_only(self):
        table = analysis.resolve_attribution(self.ledger, use_appeals=True)
        # X1 only_new_season=false：P2→T2 追溯所有季
        self.assertEqual(table["A"]["P2"], "T2")
        self.assertEqual(table["B"]["P2"], "T2")
        # X2 only_new_season=true：P6→T3 只用于 C 季
        self.assertEqual(table["B"]["P6"], "T1")
        self.assertEqual(table["C"]["P6"], "T3")

    def test_county_population_invariant_across_scenarios(self):
        # 任何一致归属情景下全县人口都必须相同，避免拼出不可能的汇总表
        for season in self.ledger.seasons:
            for use_appeals in (False, True):
                scenarios = analysis.attribution_scenarios(self.ledger, use_appeals, season)
                totals = {
                    analysis.county_population(self.ledger, scenario, season)
                    for scenario in scenarios
                }
                self.assertEqual(len(totals), 1, (season, use_appeals, totals))


class StratumTests(unittest.TestCase):
    def setUp(self):
        self.ledger = load()
        self.strata = analysis.build_strata(self.ledger)

    def test_partial_stratum_interval(self):
        stratum = self.strata[("C", "T1", "low")]
        self.assertEqual((stratum.within, stratum.unknown), (9, 6))
        self.assertEqual(stratum.status, "partial")
        self.assertAlmostEqual(stratum.point, 1.0)
        self.assertAlmostEqual(stratum.lo, 0.6)
        self.assertAlmostEqual(stratum.hi, 1.0)

    def test_full_stratum(self):
        stratum = self.strata[("C", "T1", "high")]
        self.assertEqual(stratum.status, "full")
        self.assertAlmostEqual(stratum.point, 0.5)
        self.assertAlmostEqual(stratum.lo, stratum.hi)

    def test_no_event_storms_excluded(self):
        stratum = self.strata[("C", "T1", "low")]
        self.assertEqual(stratum.confirmed, 15)  # C13/C26 为 no_event，不计入

    def test_unmeasured_and_no_danger(self):
        unmeasured = analysis.Stratum("C", "T9", "low", 3, 0, 0, 3, 0, (35,))
        self.assertEqual(unmeasured.status, "unmeasured")
        est = unmeasured.estimate()
        self.assertIsNone(est.point)
        self.assertEqual((est.lo, est.hi), (0.0, 1.0))
        no_danger = analysis.Stratum("C", "T9", "high", 0, 0, 0, 0, 0, ())
        self.assertEqual(no_danger.status, "no_danger")
        self.assertIsNone(no_danger.estimate())

    def test_standardized_level_values(self):
        # T3/C：low 点估计 1.0、high 点估计 0.4
        present = self.ledger.standards["PRESENT"]
        protect = self.ledger.standards["PROTECT"]
        est_p = analysis.village_level(self.ledger, self.strata, "C", "T3", present)
        est_q = analysis.village_level(self.ledger, self.strata, "C", "T3", protect)
        self.assertAlmostEqual(est_p.point, 0.48 + 0.52 * 0.4, places=6)
        self.assertAlmostEqual(est_q.point, 0.39 + 0.61 * 0.4, places=6)

    def test_floor_uses_same_risk_structure(self):
        floor = analysis.village_floor(self.ledger, self.strata, "C", "T3")
        self.assertAlmostEqual(floor.point, 0.4)
        self.assertTrue(analysis.floor_comparable(self.ledger, self.strata, "C", "T3"))


class TrendTests(unittest.TestCase):
    def setUp(self):
        self.ledger = load()
        self.strata = analysis.build_strata(self.ledger)

    def test_stratum_level_robust_improvement(self):
        # T2 high：B 季 4/9 → C 季 5/9，两季证据都完整，区间分离
        before = self.strata[("B", "T2", "high")].estimate()
        after = self.strata[("C", "T2", "high")].estimate()
        self.assertEqual(analysis.trend_verdict(before, after), "稳健改善")

    def test_village_trend_is_local_when_intervals_overlap(self):
        protect = self.ledger.standards["PROTECT"]
        before = analysis.village_level(self.ledger, self.strata, "B", "T3", protect)
        after = analysis.village_level(self.ledger, self.strata, "C", "T3", protect)
        self.assertEqual(analysis.trend_verdict(before, after), "点估计退步（非稳健）")
        combined = analysis.combine_verdicts(["点估计退步（非稳健）", "点估计退步（非稳健）"])
        self.assertEqual(combined, "方向一致退步（局部观察）")

    def test_flat_verdict(self):
        self.assertEqual(
            analysis.combine_verdicts(["点估计持平", "点估计持平"]), "点估计持平（非稳健）"
        )


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.ledger = load()

    def test_locked_preserved_and_caps_respected(self):
        allocations, _, _ = quota.allocate(self.ledger, "C", use_appeals=True)
        for village, alloc in allocations.items():
            budget = next(b for b in self.ledger.budgets if b.season == "C" and b.village == village)
            self.assertEqual(alloc.locked, budget.locked)
            self.assertLessEqual(alloc.approved_new, budget.total - budget.locked + 1e-9)
            self.assertLessEqual(alloc.approved_new, budget.share_limit + 1e-9)

    def test_only_genuine_gap_is_approved(self):
        allocations, _, _ = quota.allocate(self.ledger, "C", use_appeals=True)
        # T3 确有缺口 0.012（两套标准人口都支持），人口下界 50 → 0.6
        self.assertAlmostEqual(allocations["T3"].approved_new, 0.012 * 50, places=6)
        # T1/T2 区间上限不低于最低覆盖水平，确无缺口，本轮不核准
        self.assertEqual(allocations["T1"].approved_new, 0.0)
        self.assertEqual(allocations["T2"].approved_new, 0.0)
        self.assertGreater(allocations["T1"].deferred, 0.0)

    def test_naive_slice_sum_not_additive(self):
        allocations, _, _ = quota.allocate(self.ledger, "C", use_appeals=True)
        for alloc in allocations.values():
            # 可核准额度按去重人口，不按各图表下降幅度相加
            self.assertLessEqual(alloc.approved_new + alloc.deferred, alloc.naive_slice_sum + 1e-6)

    def test_shared_point_cap(self):
        import dataclasses

        ledger = self.ledger
        allocations, _, _ = quota.allocate(ledger, "C", use_appeals=True)
        for alloc in allocations.values():
            alloc.approved_new = 6.0
            alloc.deferred = 0.0
        # 构造 T1/T2 共用接收点 R0 的情形
        ledger.channels = [
            dataclasses.replace(c, common_point="R0")
            if c.season == "C" and c.village == "T1"
            else c
            for c in ledger.channels
        ]
        adjustments = quota._apply_shared_point_caps(ledger, "C", allocations)
        self.assertTrue(adjustments)
        self.assertAlmostEqual(
            allocations["T1"].approved_new + allocations["T2"].approved_new, 9.0, places=6
        )

    def test_appeal_protection_for_unproven_groups(self):
        # 构造：P5 候选含 T1，申诉把 P5 迁出 T3 → T3 人口降为 0，核准额应被保护
        with TemporaryDirectory() as directory:
            target = Path(directory) / "data"
            copytree(DATA, target)
            people_file = target / "people.csv"
            with people_file.open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            for row in rows:
                if row["group"] == "P5":
                    row["village_candidates"] = "T3|T1"
            with people_file.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
            appeals_file = target / "appeals.csv"
            with appeals_file.open("a", encoding="utf-8", newline="") as stream:
                stream.write("X9,P5,T1,C,true\n")
            ledger, issues = ledger_mod.load(target)
            self.assertEqual(issues, [])
            result = quota.compare_appeals(ledger, "C")
            before = result["before"]["T3"].approved_new
            after = result["after"]["T3"].approved_new
            self.assertGreater(before, 0.0)
            self.assertEqual(after, before)  # 缺证群组不扣额度
            self.assertTrue(result["protections"])


class ReportAndCliTests(unittest.TestCase):
    def setUp(self):
        self.ledger = load()

    def test_county_level_within_scenario_sensitivity(self):
        payload = report.season_report(self.ledger, "C", use_appeals=True)
        for name, entry in payload["全县水准"].items():
            lo, hi = entry["归属敏感范围"]
            self.assertLessEqual(lo, entry["本情景"]["点估计"] + 1e-4)
            self.assertGreaterEqual(hi, entry["本情景"]["点估计"] - 1e-4)

    def test_special_groups_distinguished(self):
        payload = report.season_report(self.ledger, "C", use_appeals=True)
        stopped = {item["群组"] for item in payload["特别分组"]["季中停用仍计入"]}
        self.assertEqual(stopped, {"P1", "P6"})
        self.assertIn("无危险过程", payload["特别分组"])
        self.assertIn("有危险无可信记录", payload["特别分组"])

    def test_group_levels_follow_attribution(self):
        payload = report.season_report(self.ledger, "C", use_appeals=True)
        levels = payload["群组水准"]
        self.assertEqual(set(levels), {"P1", "P2", "P3", "P4", "P5", "P6"})
        # 申诉情景下 P6 归 T3，其最低服务水准即 T3 的 0.4
        self.assertEqual(levels["P6"]["归属"], "T3")
        self.assertAlmostEqual(levels["P6"]["最低服务水准"]["点估计"], 0.4)
        self.assertTrue(levels["P6"]["接收点已停用"])
        baseline = report.season_report(self.ledger, "C", use_appeals=False)
        self.assertEqual(baseline["群组水准"]["P6"]["归属"], "T1")

    def test_query_contains_scope_quota_and_basis(self):
        payload = report.query_report(self.ledger, "C", village="T3")
        self.assertIn("指标适用范围", payload)
        self.assertIn("额度变动", payload)
        groups = {item["群组"] for item in payload["指标范围内群组"]}
        self.assertEqual(groups, {"P5", "P6"})
        self.assertTrue(any("区间" in line for line in payload["指标适用范围"]))
        self.assertEqual(payload["额度变动"]["计入申诉"]["已承诺保障"], 2.0)

    def test_cli_commands(self):
        for argv in (
            ["--data", str(DATA), "audit"],
            ["--data", str(DATA), "report", "--season", "C"],
            ["--data", str(DATA), "report", "--scenario", "baseline"],
            ["--data", str(DATA), "allocate"],
            ["--data", str(DATA), "appeals"],
            ["--data", str(DATA), "query", "--season", "C", "--village", "T2"],
            ["--data", str(DATA), "query", "--season", "A", "--group", "P4"],
        ):
            buffer = StringIO()
            with redirect_stdout(buffer):
                code = main(argv)
            self.assertEqual(code, 0, argv)
            json.loads(buffer.getvalue())

    def test_cli_rejects_unknown_season(self):
        buffer = StringIO()
        with redirect_stdout(buffer):
            code = main(["--data", str(DATA), "query", "--season", "Z"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
