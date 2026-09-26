import sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, BatchService, Store


class BatchFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.s = BatchService(Store(Path(self.tmp.name) / "b.db"))
        self.f1 = self.s.register_factory("qa", "qa", "F1", "一厂", "CN")["id"]
        self.f2 = self.s.register_factory("qa", "qa", "F2", "二厂", "CN")["id"]
        self.future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat().replace("+00:00", "Z")

    def tearDown(self): self.s.store.close(); self.tmp.cleanup()

    def test_full_investigation_retest_rework_and_release(self):
        batch = self.s.create_batch("operator", "operator", self.f1, "B-1", "药片", "2026-01-01", "2028-01-01")
        dev = self.s.add_deviation("operator", "operator", self.f1, batch["id"], "minor", "装量轻微偏离", self.future, batch["revision"])
        failed = self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 89, 95, 105, dev["batch_id"] and self.s.batch_detail(batch["id"])["batch"]["revision"])
        self.assertFalse(failed["passed"])
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        passed = self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 99, 95, 105, current)
        self.assertTrue(passed["passed"])
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        self.s.close_deviation("qa", "qa", dev["id"], "调整灌装参数", current)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        rw = self.s.plan_rework("operator", "operator", self.f1, batch["id"], "返工包装", current)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        self.s.complete_rework("operator", "operator", self.f1, rw["id"], current)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        self.s.record_stability("lab", "lab", self.f1, batch["id"], "25C/60RH", "3m", 99, 105, current)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        result = self.s.decide("qa", "qa", batch["id"], "release", "调查关闭，复测合格", current)
        self.assertEqual("released", result["batch"]["state"])
        self.assertEqual(1, len(result["batch"] and self.s.batch_detail(batch["id"])["decisions"]))

    def test_critical_block_conditional_exception_and_factory_conflict(self):
        batch = self.s.create_batch("operator", "operator", self.f1, "B-2", "胶囊", "2026-02-01", "2028-02-01")
        current = batch["revision"]
        self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 100, 95, 105, current)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        crit = self.s.add_deviation("inspector", "inspector", self.f1, batch["id"], "critical", "无菌数据异常", self.future, current)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        with self.assertRaises(ApiError) as blocked:
            self.s.decide("qa", "qa", batch["id"], "release", "尝试放行", current)
        self.assertIn("关键偏差", blocked.exception.message)
        with self.assertRaises(ApiError):
            self.s.approve_exception("qa", "qa", crit["id"], "暂时接受", self.future, current)
        with self.assertRaises(ApiError):
            self.s.record_test("lab", "lab", self.f2, batch["id"], "水分", 1, 0, 2, current)
        with self.assertRaises(ApiError) as stale:
            self.s.record_test("lab", "lab", self.f1, batch["id"], "水分", 1, 0, 2, 1)
        self.assertEqual(409, stale.exception.status)

    def _released_batch(self, batch_no="B-3"):
        batch = self.s.create_batch("operator", "operator", self.f1, batch_no, "注射液", "2026-03-01", "2028-03-01")
        self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 100, 95, 105, batch["revision"])
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        self.s.decide("qa", "qa", batch["id"], "release", "检验合格，正式放行", current)
        return self.s.batch_detail(batch["id"])["batch"]

    def test_release_creates_summary_and_supplement_adds_version(self):
        batch = self._released_batch()
        summaries = self.s.summaries(batch["id"])
        self.assertEqual(1, len(summaries))
        self.assertEqual("release", summaries[0]["source"])
        self.assertEqual("released", summaries[0]["snapshot"]["batch"]["state"])
        rec = self.s.submit_post_release("lab", "lab", self.f1, batch["id"], "stability",
                                         {"condition": "25C/60RH", "timepoint": "6m", "result": 98, "spec_limit": 105}, batch["revision"])
        self.assertEqual("pending", rec["status"])
        self.assertEqual(1, len(self.s.pending_post_release()))
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        result = self.s.assess_post_release("qa", "qa", rec["id"], "pass", "长期稳定性数据符合标准", current)
        self.assertEqual("accepted", result["record"]["status"])
        self.assertEqual("released", result["batch"]["state"])
        summaries = self.s.summaries(batch["id"])
        self.assertEqual(2, len(summaries))
        self.assertEqual("supplement", summaries[1]["source"])
        self.assertEqual(1, len(summaries[1]["snapshot"]["supplementary_evidence"]))
        self.assertEqual(0, len(self.s.pending_post_release()))

    def test_failed_supplement_triggers_recall_review_and_keeps_release_record(self):
        batch = self._released_batch("B-4")
        rec = self.s.submit_post_release("lab", "lab", self.f1, batch["id"], "stability",
                                         {"condition": "25C/60RH", "timepoint": "9m", "result": 108, "spec_limit": 105}, batch["revision"])
        self.assertFalse(rec["passed"])
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        with self.assertRaises(ApiError) as forced:
            self.s.assess_post_release("qa", "qa", rec["id"], "pass", "试图按合格处理", current)
        self.assertIn("不合格", forced.exception.message)
        result = self.s.assess_post_release("qa", "qa", rec["id"], "fail", "稳定性超标，影响有效期", current)
        self.assertEqual("recall_review", result["batch"]["state"])
        detail = self.s.batch_detail(batch["id"])
        self.assertEqual("release", detail["decisions"][-1]["decision"])
        self.assertEqual("released", detail["summaries"][0]["snapshot"]["batch"]["state"])
        with self.assertRaises(ApiError):
            self.s.decide("qa", "qa", batch["id"], "release", "重复放行", detail["batch"]["revision"])
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        with self.assertRaises(ApiError):
            self.s.review_recall("operator", "operator", batch["id"], "clear", "无权复核", current)
        cleared = self.s.review_recall("qa", "qa", batch["id"], "clear", "调查确认为孤立超标，留样复测合格，维持放行", current)
        self.assertEqual("released", cleared["state"])
        detail = self.s.batch_detail(batch["id"])
        self.assertEqual("closed", detail["post_release"][0]["status"])
        self.assertIn("维持放行", detail["post_release"][0]["conclusion"])
        self.assertEqual("recall_review", detail["summaries"][-1]["source"])

    def test_critical_deviation_supplement_must_enter_recall_and_can_be_recalled(self):
        batch = self._released_batch("B-5")
        rec = self.s.submit_post_release("inspector", "inspector", self.f1, batch["id"], "deviation",
                                         {"severity": "critical", "title": "留样发现包装密封缺陷"}, batch["revision"])
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        with self.assertRaises(ApiError) as blocked:
            self.s.assess_post_release("qa", "qa", rec["id"], "pass", "试图按合格处理", current)
        self.assertIn("关键偏差", blocked.exception.message)
        self.s.assess_post_release("qa", "qa", rec["id"], "fail", "关键偏差，启动召回评估", current)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        recalled = self.s.review_recall("qa", "qa", batch["id"], "recall", "确认密封缺陷，执行召回", current)
        self.assertEqual("recalled", recalled["state"])
        with self.assertRaises(ApiError):
            self.s.submit_post_release("lab", "lab", self.f1, batch["id"], "test",
                                       {"test_type": "含量", "result": 99, "spec_min": 95, "spec_max": 105},
                                       self.s.batch_detail(batch["id"])["batch"]["revision"])

    def test_post_release_requires_released_batch_and_current_revision(self):
        batch = self.s.create_batch("operator", "operator", self.f1, "B-6", "药片", "2026-01-01", "2028-01-01")
        with self.assertRaises(ApiError) as not_released:
            self.s.submit_post_release("lab", "lab", self.f1, batch["id"], "test",
                                       {"test_type": "含量", "result": 99, "spec_min": 95, "spec_max": 105}, batch["revision"])
        self.assertEqual(409, not_released.exception.status)
        released = self._released_batch("B-7")
        with self.assertRaises(ApiError) as stale:
            self.s.submit_post_release("lab", "lab", self.f1, released["id"], "test",
                                       {"test_type": "含量", "result": 99, "spec_min": 95, "spec_max": 105}, 1)
        self.assertEqual(409, stale.exception.status)
        with self.assertRaises(ApiError):
            self.s.submit_post_release("lab", "lab", self.f2, released["id"], "test",
                                       {"test_type": "含量", "result": 99, "spec_min": 95, "spec_max": 105}, released["revision"])


if __name__ == "__main__": unittest.main()
