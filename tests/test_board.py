"""只测数据整理与渲染尺寸：不访问网络、不连接设备。"""
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import board  # noqa: E402


def iso(seconds_ago):
    return datetime.fromtimestamp(time.time() - seconds_ago, timezone.utc).isoformat().replace("+00:00", "Z")


def raw_run(conclusion, hours_ago, name="build", branch="main"):
    return {"conclusion": conclusion, "created_at": iso(hours_ago * 3600),
            "name": name, "head_branch": branch}


class TestFailures(unittest.TestCase):
    def test_only_failures_within_week(self):
        runs = [
            raw_run("failure", 1),          # 保留
            raw_run("success", 1),          # 成功，丢弃
            raw_run("failure", 24 * 8),     # 超过 7 天，丢弃
            raw_run("cancelled", 2),        # 取消，丢弃
        ]
        out = board.runs_to_failures("a/b", runs)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["repo"], "a/b")
        self.assertEqual(out[0]["workflow"], "build")

    def test_missing_fields_are_tolerated(self):
        out = board.runs_to_failures("a/b", [{"conclusion": "failure", "created_at": iso(60)}])
        self.assertEqual(out[0]["workflow"], "workflow")
        self.assertEqual(out[0]["branch"], "?")

    def test_sorted_by_time_desc(self):
        runs = [raw_run("failure", 10), raw_run("failure", 1), raw_run("failure", 5)]
        items = board.runs_to_failures("a/b", runs)
        items.sort(key=lambda x: board._sort_key(x["time"]), reverse=True)
        self.assertGreater(board._sort_key(items[0]["time"]),
                           board._sort_key(items[-1]["time"]))


class TestSummary(unittest.TestCase):
    def test_counts_running_and_last_success(self):
        runs = [
            {"status": "in_progress", "conclusion": None, "created_at": iso(60)},
            {"status": "completed", "conclusion": "success", "created_at": iso(600)},
            {"status": "completed", "conclusion": "failure", "created_at": iso(1200)},
        ]
        out = board.summarize_runs(runs)
        self.assertEqual(out["running"], 1)
        self.assertEqual(out["recent"][0], "running")
        self.assertEqual(out["recent"][1], "success")
        self.assertIsNotNone(out["last_success"])

    def test_no_runs(self):
        out = board.summarize_runs([])
        self.assertEqual(out, {"running": 0, "last_success": None, "recent": []})


class TestReviews(unittest.TestCase):
    def test_maps_fields(self):
        items = [{"title": "fix: x", "number": 3, "updated_at": iso(600),
                  "user": {"login": "alice"}}]
        out = board.search_to_reviews("a/b", items)
        self.assertEqual(out[0]["title"], "fix: x")
        self.assertEqual(out[0]["author"], "alice")
        self.assertEqual(out[0]["number"], 3)

    def test_empty(self):
        self.assertEqual(board.search_to_reviews("a/b", []), [])


class TestRender(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(board.BASE, "fixture.json"), encoding="utf-8") as f:
            self.data = json.load(f)

    def test_two_pages_are_400x300(self):
        for page in (0, 1):
            with tempfile.TemporaryDirectory() as tmp:
                out = os.path.join(tmp, "p.png")
                img = board.Image.new("L", (400, 300), 255)
                d = board.ImageDraw.Draw(img)
                (board.page_overview if page == 0 else board.page_detail)(d, self.data)
                img.save(out)
                from PIL import Image
                got = Image.open(out)
                self.assertEqual(got.size, (400, 300))
                self.assertEqual(got.mode, "L")
                self.assertGreater(os.path.getsize(out), 1000)

    def test_empty_data_renders(self):
        empty = {"fetched_at": time.time(), "failures": [], "reviews": [], "stale": False, "demo": True}
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "p.png")
            self.assertTrue(board.render(0, empty, out))
            self.assertEqual(board.Image.open(out).size, (400, 300))

    def test_render_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "p.png")
            self.assertTrue(board.render(0, self.data, out))
            self.assertFalse(board.render(0, self.data, out))  # 内容未变不重写


class TestFixture(unittest.TestCase):
    def test_fixture_marks_demo(self):
        data = board.load_fixture()
        self.assertTrue(data["demo"])
        self.assertIn("failures", data)
        self.assertIn("reviews", data)


if __name__ == "__main__":
    unittest.main()
