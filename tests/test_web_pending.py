"""待处理视频：三处判据怎么读成同一张清单，以及五个动作各做什么。

判据分别住在诊断、媒体修复的扫描缓存与扫描采集的问题日志里，各有各的生命周期；
这一页只是把它们投影成同一行形状。动作这边最要紧的是两件事：忽略只写
`review_decision`，移入回收站只转交 `w_batch`——自己不写 `asset.disposal`。
"""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from peach import media_sources, web_batch, web_media_repair, web_pending, web_state
from peach.web_media_repair import CLEAN, HEADER, INDEX, UNAVAILABLE

from support.ledger import fresh_ledger

SEP = chr(92)
LOCAL = "local"
CLOUD = "115"


class Config:
    """设置对象：两个来源各一个根，都是不限流量的那一类。"""

    def __init__(self, root: Path):
        self.data_root = root
        self.path = root / "config.toml"
        self.locations = {LOCAL: ("R:" + SEP + "Media",), CLOUD: ("B:" + SEP,)}
        self.library_names = {"R:" + SEP + "Media": "本地", "B:" + SEP: "网盘"}
        self.media_costs = {}
        self.directories = {"state": "state"}

    def directory(self, key: str) -> Path:
        path = self.data_root / (self.directories.get(key) or key)
        path.mkdir(parents=True, exist_ok=True)
        return path


class PendingVideosTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.db = fresh_ledger(self.root)
        self.cache_root = self.root / "transcodes"
        self.cache_root.mkdir()
        self.contract = web_state.WebContract(self.db, transcode_root=self.cache_root)
        self.addCleanup(self.contract.stop_background_jobs)
        self.config = Config(self.root)
        patch = mock.patch.object(web_pending.settings_file, "active", lambda: self.config)
        patch.start()
        self.addCleanup(patch.stop)

    # ---- 素材 -----------------------------------------------------------------

    def add(self, asset_id: int, *, medium="video", duration=None, location=LOCAL,
            name=None, disposal=None, size=1000, code=None, title=None):
        name = name or f"{asset_id}.mp4"
        prefix = ("R:" + SEP + "Media" + SEP) if location == LOCAL else ("B:" + SEP)
        connection = sqlite3.connect(self.db)
        try:
            connection.execute(
                "INSERT INTO asset(id,location,path,name,medium,size,duration,disposal,code,catalog_title) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (asset_id, location, prefix + name, name, medium, size, duration, disposal, code, title))
            connection.commit()
        finally:
            connection.close()

    def ignore(self, kind: str, asset_id: int) -> None:
        web_pending.w_pending_action(self.contract, {"action": "ignore", "ids": [asset_id], "kinds": [kind]})

    def scan_cache(self, entries: dict[str, str]) -> None:
        (self.cache_root / web_media_repair.SCAN_CACHE_NAME).write_text(
            json.dumps({"version": 1, "entries": entries}), encoding="utf-8")

    def issues(self, job_id: str, records) -> None:
        state = {"job_id": job_id, "status": "failed", "retryable_asset_ids": [2]}
        (self.config.directory("state") / "library-processing.json").write_text(
            json.dumps(state), encoding="utf-8")
        lines = "\n".join(json.dumps(record) for record in records) + "\n"
        (self.config.directory("state") / f"library-processing-{job_id}.issues.jsonl").write_text(
            lines, encoding="utf-8")

    def query(self, **args):
        return web_pending.q_pending_videos(self.contract, args)

    def rows(self, sql: str, parameters=()):
        """直接读账本：结论要落在文件里，不能只看投影怎么说。"""
        with closing(sqlite3.connect(self.db)) as connection:
            return connection.execute(sql, parameters).fetchall()

    # ---- 投影 -----------------------------------------------------------------

    def test_a_video_without_a_duration_is_pending_and_reprobe_is_allowed(self):
        self.add(1, duration=None, code="ABC-001", title="第一部")
        self.add(2, duration=3600, code="ABC-002")

        data = self.query()

        self.assertEqual([item["id"] for item in data["items"]], [1])
        item = data["items"][0]
        self.assertEqual((item["kind"], item["reprobe"], item["blocked_reason"]),
                         ("duration", True, ""))
        self.assertEqual((item["code"], item["title"], item["library"]), ("ABC-001", "第一部", "本地"))
        self.assertEqual(data["counts"]["duration"]["total"], 1)
        self.assertEqual(data["total"], 1)

    def test_trash_vanished_and_non_video_rows_are_not_pending(self):
        self.add(1, duration=None, disposal="trash")
        self.add(2, duration=None, disposal="vanished")
        self.add(3, duration=None, medium="image")
        self.add(4, duration=0)

        data = self.query()

        self.assertEqual([item["id"] for item in data["items"]], [4], "探过但没拿到时长的仍要人看一眼")
        self.assertFalse(data["items"][0]["reprobe"])
        self.assertIn("scripts/probe.py --redo failed", data["items"][0]["blocked_reason"])

    def test_a_metered_source_is_listed_but_never_reprobed_here(self):
        """计费来源上重探等于把整片拉一遍，这一页不替人做。"""
        self.config.locations[CLOUD] = ("B:" + SEP,)
        self.add(1, duration=None, location=CLOUD)
        with mock.patch.object(media_sources, "SOURCE_COSTS", {**media_sources.SOURCE_COSTS, CLOUD: "metered"}):
            data = self.query()

        self.assertEqual(data["items"][0]["kind"], "duration")
        self.assertFalse(data["items"][0]["reprobe"])
        self.assertIn("计费", data["items"][0]["blocked_reason"])

    def test_strm_pointers_and_iso_images_are_not_reprobed(self):
        self.add(1, duration=None, name="pointer.strm")
        self.add(2, duration=None, name="disc.iso")
        data = self.query()
        for item in data["items"]:
            with self.subTest(name=item["name"]):
                self.assertFalse(item["reprobe"])
                self.assertIn("STRM", item["blocked_reason"])

    def test_the_media_repair_cache_is_only_trusted_while_the_file_size_matches(self):
        self.add(1, duration=3600, size=1000, code="ABC-001")
        self.add(2, duration=3600, size=1000)
        self.scan_cache({"1-1000-77": HEADER, "2-999-77": INDEX, "3-1000-77": UNAVAILABLE})

        data = self.query(kind="media")

        self.assertEqual([item["id"] for item in data["items"]], [1], "文件换了大小，旧结论不作数")
        item = data["items"][0]
        self.assertEqual(item["detail"], web_pending.MEDIA_DETAIL[HEADER])
        self.assertFalse(item["reprobe"])
        self.assertIn("媒体修复", item["blocked_reason"])

    def test_clean_verdicts_are_not_pending(self):
        self.add(1, duration=3600)
        self.scan_cache({"1-1000-77": CLEAN})
        self.assertEqual(self.query(kind="media")["items"], [])

    def test_the_issue_log_is_read_without_its_informational_lines(self):
        self.add(1, duration=3600)
        self.add(2, duration=3600)
        self.add(3, duration=3600)
        note = next(iter(web_pending.NOTE_KEYS))
        self.issues("job-1", [
            {"asset_id": 1, "message": note, "severity": "error"},
            {"asset_id": 2, "message": "封面下载超时", "severity": "error"},
            {"asset_id": 3, "message": "元数据没取到", "severity": "error"},
        ])

        data = self.query(kind="processing")

        self.assertEqual([item["id"] for item in data["items"]], [2, 3], "告知项不是问题")
        retryable = next(item for item in data["items"] if item["id"] == 2)
        blocked = next(item for item in data["items"] if item["id"] == 3)
        self.assertTrue(retryable["retryable"])
        self.assertEqual(retryable["job_id"], "job-1")
        self.assertFalse(blocked["retryable"])
        self.assertIn("可重试", blocked["blocked_reason"])

    def test_a_missing_issue_log_leaves_the_other_kinds_alone(self):
        self.add(1, duration=None)
        (self.config.directory("state") / "library-processing.json").write_text(
            json.dumps({"job_id": "gone", "status": "failed"}), encoding="utf-8")
        data = self.query()
        self.assertEqual([item["kind"] for item in data["items"]], ["duration"])

    def test_counts_are_reported_per_kind_and_sum_to_the_list(self):
        self.add(1, duration=None)
        self.add(2, duration=None, name="pointer.strm")
        self.add(3, duration=3600, size=1000)
        self.scan_cache({"3-1000-77": INDEX})

        data = self.query()

        self.assertEqual({kind: data["counts"][kind]["total"] for kind in web_pending.KINDS},
                         {"duration": 2, "media": 1, "processing": 0})
        self.assertEqual(data["counts"]["duration"]["reprobe"], 1)
        self.assertEqual(data["counts"]["duration"]["blocked"], 1)
        self.assertEqual(data["total"], 3)

    def test_a_kind_filter_only_changes_which_rows_are_listed(self):
        self.add(1, duration=None)
        self.add(2, duration=3600, size=1000)
        self.scan_cache({"2-1000-77": INDEX})

        data = self.query(kind="media")

        self.assertEqual([item["id"] for item in data["items"]], [2])
        self.assertEqual(data["counts"]["duration"]["total"], 1, "分项读数不跟着筛选走")
        with self.assertRaisesRegex(ValueError, "没有这一类问题"):
            self.query(kind="unknown")

    def test_counts_only_leaves_the_rows_out(self):
        self.add(1, duration=None)
        data = self.query(counts="1")
        self.assertEqual(data["items"], [])
        self.assertNotIn("job", data)
        self.assertEqual(data["total"], 1)

    # ---- 忽略 -----------------------------------------------------------------

    def test_ignoring_writes_only_the_review_decision(self):
        self.add(1, duration=None)
        self.ignore("duration", 1)

        row = self.rows("SELECT category,item_key,status FROM review_decision")
        self.assertEqual(row, [(web_pending.IGNORE_CATEGORY, "duration:1", "skipped")])
        asset = self.rows("SELECT disposal,feedback FROM asset WHERE id=1")
        self.assertEqual(asset, [(None, None)], "忽略不是对馆藏行做任何改动")
        self.assertEqual(self.query()["items"], [])
        self.assertEqual(self.query()["ignored_total"], 1)

    def test_an_ignored_row_comes_back_when_asked_for(self):
        self.add(1, duration=None)
        self.ignore("duration", 1)

        listed = self.query(include_ignored="1")["items"]

        self.assertEqual([item["id"] for item in listed], [1])
        self.assertTrue(listed[0]["ignored"])
        # 条目的 `reprobe` 讲的是「这一类能不能自动重探」，与忽略与否无关；被忽略的那几条
        # 不进这一页的读数与批量动作（`counts`／`reprobe`），所以两者分得开。
        self.assertEqual(self.query(include_ignored="1")["reprobe"], 0)
        self.assertEqual(self.query(include_ignored="1")["total"], 1)

    def test_unignore_deletes_the_decision(self):
        self.add(1, duration=None)
        self.ignore("duration", 1)
        web_pending.w_pending_action(self.contract, {"action": "unignore", "ids": [1], "kinds": ["duration"]})

        self.assertEqual(self.rows("SELECT COUNT(*) FROM review_decision"),
                         [(0,)], "恢复提示就是把那一条决策删掉")
        self.assertEqual(len(self.query()["items"]), 1)

    def test_ignore_is_per_kind_so_the_same_asset_can_show_another_problem(self):
        self.add(1, duration=None, size=1000)
        self.scan_cache({"1-1000-77": INDEX})
        self.ignore("duration", 1)

        data = self.query()

        self.assertEqual([item["kind"] for item in data["items"]], ["media"])

    def test_ignoring_needs_the_kind_and_a_bounded_selection(self):
        self.add(1, duration=None)
        with self.assertRaisesRegex(ValueError, "问题类型"):
            web_pending.w_pending_action(self.contract, {"action": "ignore", "ids": [1]})
        with self.assertRaisesRegex(ValueError, "问题类型"):
            web_pending.w_pending_action(self.contract, {"action": "ignore", "ids": [1], "kinds": ["nope"]})
        many = list(range(1, web_pending.ACTION_LIMIT + 2))
        with self.assertRaisesRegex(ValueError, "1 到"):
            web_pending.w_pending_action(self.contract, {"action": "ignore", "ids": many, "kinds": ["duration"]})
        with self.assertRaisesRegex(TypeError, "ids"):
            web_pending.w_pending_action(self.contract, {"action": "ignore", "kinds": ["duration"]})

    # ---- 移入回收站 ------------------------------------------------------------

    def test_dispose_is_delegated_to_the_batch_implementation(self):
        self.add(1, duration=None)
        self.add(2, duration=None)
        with mock.patch.object(web_pending, "w_batch", wraps=web_batch.w_batch) as batch:
            result = web_pending.w_pending_action(self.contract, {"action": "dispose", "ids": [1, 2]})

        # 回执就是批量那一条实现给的那一份：这一页自己不写 `asset.disposal`。
        self.assertEqual(result["result"], {"ok": True, "operation": "dispose", "changed": 2})
        batch.assert_called_once_with(self.contract, {"ids": [1, 2], "operation": "dispose",
                                                      "disposal_reason": "manual"})
        rows = self.rows("SELECT id,disposal,disposal_reason FROM asset ORDER BY id")
        self.assertEqual(rows, [(1, "trash", "manual"), (2, "trash", "manual")])
        self.assertEqual(self.query()["items"], [], "进了回收站的片子不再待处理")

    # ---- 重新探测 --------------------------------------------------------------

    def test_reprobe_runs_in_the_background_and_groups_the_selection_by_source_root(self):
        """后台任务一次只认一个来源根，所以选中的 id 要先按「来源 + 根」分开再串行跑。"""
        self.add(1, duration=None)
        self.add(2, duration=None)
        self.add(3, duration=None, name="pointer.strm")
        calls, started = [], []

        def probe(db_path, ffprobe, location, root, *, asset_ids, config, report):
            calls.append((location, root, list(asset_ids)))
            report(0, len(asset_ids))
            return len(asset_ids)

        with mock.patch.object(web_pending, "FFmpegResolver") as resolver, \
                mock.patch.object(web_pending.media_probe, "probe_unmeasured", side_effect=probe), \
                mock.patch.object(self.contract, "cache_bust"):
            resolver.return_value.ffprobe.return_value = mock.Mock(path=Path("ffprobe"))
            # 真的起后台线程会把探测结果写进账本；这里只要看它交出去的那一趟做了什么。
            def run(fn, **_kwargs):
                started.append(fn)
                return {"job_id": "j1"}
            job = self.contract.pending_reprobe_job
            with mock.patch.object(job, "start", side_effect=run) as start, \
                    mock.patch.object(job, "snapshot", return_value={"job_id": "j1", "status": "running"}), \
                    mock.patch.object(job, "update") as update:
                result = web_pending.w_pending_action(self.contract, {"action": "reprobe", "ids": [1, 2, 3]})

                self.assertEqual(result["job_id"], "j1")
                options = start.call_args.kwargs
                self.assertTrue(options["restart"])
                self.assertEqual(options["recovery"], {"action": "reprobe", "ids": [1, 2]},
                                 "恢复记录里只留真的会去探测的那些")
                self.assertEqual(calls, [], "交给后台之前不该已经在当前线程里探测")

                # 后台那一趟：按分组逐个来源根串行探测，跑完把状态推成完成。
                started[0]("j1")
                self.assertEqual(update.call_args.kwargs["status"], "complete")

        self.assertEqual(calls, [(LOCAL, "R:" + SEP + "Media", [1, 2])])

    def test_reprobe_refuses_a_selection_in_which_nothing_can_be_probed(self):
        self.add(1, duration=3600)
        self.add(2, duration=None, name="pointer.strm")
        with self.assertRaisesRegex(ValueError, "可重新探测"):
            web_pending.w_pending_action(self.contract, {"action": "reprobe", "ids": [1, 2]})

    # ---- 未知动作 --------------------------------------------------------------

    def test_an_unknown_action_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "不需要处理的动作"):
            web_pending.w_pending_action(self.contract, {"action": "delete"})
