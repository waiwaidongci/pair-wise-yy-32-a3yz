import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, BatchService, Store


class PostReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.s = BatchService(Store(Path(self.tmp.name) / "b.db"))
        self.f1 = self.s.register_factory("qa", "qa", "F1", "一厂", "CN")["id"]
        batch = self.s.create_batch("op", "operator", self.f1, "B-1", "药片", "2026-01-01", "2028-01-01")
        self.bid = batch["id"]
        self.s.record_test("lab", "lab", self.f1, self.bid, "含量", 99, 95, 105, batch["revision"])
        self.s.decide("qa", "qa", self.bid, "release", "检验合格，正式放行", self.rev())

    def tearDown(self): self.s.store.close(); self.tmp.cleanup()

    def rev(self): return self.s.batch_detail(self.bid)["batch"]["revision"]

    def test_release_creates_summary_and_acceptable_evidence_adds_version(self):
        summary = self.s.batch_summary(self.bid)
        self.assertEqual(1, len(summary["versions"]))
        self.assertEqual("released", summary["versions"][0]["snapshot"]["batch"]["state"])
        item = self.s.submit_post_release("lab", "lab", self.f1, self.bid, "stability",
                                          {"condition": "25C/60RH", "timepoint": "6m", "result": 98, "spec_limit": 105}, self.rev())
        self.assertEqual("pending", item["status"])
        out = self.s.assess_post_release("qa", "qa", item["id"], "acceptable", "长期稳定性数据符合标准", self.rev())
        self.assertEqual("accepted", out["item"]["status"])
        self.assertEqual("released", out["batch"]["state"])
        summary = self.s.batch_summary(self.bid)
        self.assertEqual(2, len(summary["versions"]))
        evidence = summary["versions"][1]["snapshot"]["post_release_evidence"]
        self.assertEqual(1, len(evidence))
        self.assertEqual("stability", evidence[0]["data_type"])
        self.assertEqual([], summary["pending"])

    def test_failing_data_forces_recall_review_and_clear_restores_state(self):
        item = self.s.submit_post_release("lab", "lab", self.f1, self.bid, "test",
                                          {"test_type": "含量", "result": 90, "spec_min": 95, "spec_max": 105}, self.rev())
        # 评估人即使认为可接受，不合格数据仍强制进入召回待审
        out = self.s.assess_post_release("qa", "qa", item["id"], "acceptable", "复测仍不合格", self.rev())
        self.assertEqual("recall_pending", out["item"]["status"])
        self.assertEqual("recall_review", out["batch"]["state"])
        self.assertEqual("released", out["batch"]["recall_from_state"])
        res = self.s.review_recall("qa", "qa", self.bid, "clear", "留样复测合格，解除待审", self.rev())
        self.assertEqual("released", res["batch"]["state"])
        items = self.s.batch_summary(self.bid)["items"]
        self.assertEqual("cleared", items[0]["status"])
        self.assertEqual("留样复测合格，解除待审", items[0]["conclusion"])
        self.assertEqual("qa", items[0]["reviewed_by"])

    def test_critical_deviation_recall_preserves_release_records(self):
        item = self.s.submit_post_release("inspector", "inspector", self.f1, self.bid, "deviation",
                                          {"severity": "critical", "title": "无菌保证失效"}, self.rev())
        self.s.assess_post_release("qa", "qa", item["id"], "unacceptable", "触及关键偏差", self.rev())
        res = self.s.review_recall("qa", "qa", self.bid, "recall", "启动召回", self.rev())
        self.assertEqual("recalled", res["batch"]["state"])
        detail = self.s.batch_detail(self.bid)
        self.assertEqual("release", detail["decisions"][0]["decision"])  # 原放行记录保留
        self.assertEqual("recalled", detail["post_release_items"][0]["status"])
        versions = self.s.batch_summary(self.bid)["versions"]
        self.assertGreaterEqual(len(versions), 2)
        self.assertIn("确认召回", versions[-1]["note"])
        with self.assertRaises(ApiError) as blocked:
            self.s.record_test("lab", "lab", self.f1, self.bid, "水分", 1, 0, 2, self.rev())
        self.assertEqual(409, blocked.exception.status)

    def test_guards(self):
        with self.assertRaises(ApiError) as not_released:
            self.s.submit_post_release("lab", "lab", self.f1, self.bid, "test",
                                       {"test_type": "含量", "result": 99, "spec_min": 95, "spec_max": 105}, 999)
        self.assertEqual(409, not_released.exception.status)
        item = self.s.submit_post_release("lab", "lab", self.f1, self.bid, "test",
                                          {"test_type": "含量", "result": 99, "spec_min": 95, "spec_max": 105}, self.rev())
        with self.assertRaises(ApiError):
            self.s.assess_post_release("lab", "lab", item["id"], "acceptable", "越权评估", self.rev())
        with self.assertRaises(ApiError):
            self.s.review_recall("qa", "qa", self.bid, "clear", "未在待审", self.rev())
        self.s.assess_post_release("qa", "qa", item["id"], "acceptable", "补充检验合格", self.rev())
        with self.assertRaises(ApiError) as done:
            self.s.assess_post_release("qa", "qa", item["id"], "acceptable", "重复评估", self.rev())
        self.assertEqual(409, done.exception.status)


if __name__ == "__main__": unittest.main()
