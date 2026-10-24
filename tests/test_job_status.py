from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "job_status.py"
SPEC = importlib.util.spec_from_file_location("peach_job_status", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class JobStatusTests(unittest.TestCase):
    def test_hooks_name_an_explicit_interpreter(self) -> None:
        """hook 不许用裸 `python`。

        这台机器上裸 `python` 曾解析到 Microsoft Store 的 MSIX 别名：路径存在、
        执行报 FileNotFoundError，而 hook 的失败没人盯着看。写成项目内的
        `.venv` 解释器，缺了就是缺了，不会解析到另一个 Python。

        `uv` 是唯一另一个允许出现的命令。它不是 `python`，顶不到那个别名上；而它那
        一条的整条命令由 `tests/test_release_due.py` 原样执行一遍，PATH 上没有它就是
        测试红，不是 hook 静默。走 `uv` 是因为那条 hook 要在 Windows 和 macOS 上都
        跑，而 hook 配置不支持按平台分支。"""
        settings = json.loads(
            (SCRIPT.parents[1] / ".claude" / "settings.json").read_text(encoding="utf-8")
        )
        commands = {
            hook["command"]
            for event in settings["hooks"].values()
            for matcher in event
            for hook in matcher["hooks"]
        }
        self.assertEqual(commands,
                         {"${CLAUDE_PROJECT_DIR}/.venv/Scripts/python.exe", "uv"})
        # 每条都带 `-X utf8`：这几个脚本打的都是中文，而 Windows 上解释器的输出端
        # 默认落在 gbk，读的一端按 UTF-8 解，屏幕上就只剩命令名和数字认得出来。
        for event in settings["hooks"].values():
            for matcher in event:
                for hook in matcher["hooks"]:
                    args = hook.get("args", [])
                    self.assertIn("-X", args, hook)
                    self.assertEqual(args[args.index("-X") + 1], "utf8", hook)
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            cwd=SCRIPT.parents[1],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hook_event_is_sanitized_and_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "state" / "handoff.json"
            MODULE.record_hook_event(state, {
                "hook_event_name": "StopFailure",
                "transcript_path": "secret transcript",
                "access_token": "never persist me",
            })
            saved = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(saved["agent"], "claude")
            self.assertEqual(saved["event"], "StopFailure")
            self.assertEqual(saved["outcome"], "failed")
            self.assertNotIn("access_token", saved)
            self.assertNotIn("transcript_path", saved)
            self.assertFalse(list(state.parent.glob("*.tmp")))

    def test_rendered_times_are_local_while_stored_times_stay_utc(self) -> None:
        """时间按本地时区渲染，存进 state 的仍是 UTC。"""
        moment = datetime.fromisoformat(MODULE._local("2026-08-29T02:51:51+00:00"))
        self.assertEqual(
            moment, datetime(2026, 8, 29, 2, 51, 51, tzinfo=timezone.utc),
            "换算只能改变显示的时区，不能改变它指的时刻",
        )
        self.assertEqual(
            moment.utcoffset(), datetime.now().astimezone().utcoffset(),
            "渲染出的时间要带本机时区偏移",
        )
        self.assertEqual(MODULE._local("说不清"), "说不清", "解析不了就原样显示")

        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "state" / "handoff.json"
            MODULE.record_hook_event(state, {"hook_event_name": "Stop"})
            stored = json.loads(state.read_text(encoding="utf-8"))["at"]
        self.assertEqual(
            datetime.fromisoformat(stored).utcoffset(),
            timedelta(0),
            "存储一律 UTC，这一半不能跟着显示口径走",
        )

    def test_the_generated_block_lands_outside_the_repository(self) -> None:
        """自动区块使用配置中的 Peach 数据目录，保存在仓库外。"""
        from peach.config import STATE_DIR

        self.assertEqual(MODULE.DOC, STATE_DIR / "job-status.md")
        self.assertEqual(MODULE.STATE, STATE_DIR / "agent-handoff.json")
        repo = SCRIPT.parents[1]
        self.assertNotIn(repo, MODULE.DOC.parents, "自动产物不能落在仓库里")

    def test_write_back_creates_the_document_when_it_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            doc = Path(root) / "state" / "job-status.md"
            self.assertEqual(MODULE.write_back(doc, [MODULE.START, "fresh", MODULE.END]),
                             "新建了区块")
            self.assertEqual(MODULE.write_back(doc, [MODULE.START, "again", MODULE.END]),
                             "替换了已有区块")
            text = doc.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("# 批处理进度（自动生成）"), text[:40])
            self.assertIn("again", text)
            self.assertNotIn("fresh", text)

    def test_write_back_replaces_only_managed_block(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            tmp_path = Path(root)
            doc = tmp_path / "STATUS.md"
            doc.write_text("before\n\n<!-- job-status:start -->\nold\n<!-- job-status:end -->\n\nafter\n", encoding="utf-8")
            block = [MODULE.START, "fresh", MODULE.END]
            self.assertEqual(MODULE.write_back(doc, block), "替换了已有区块")
            self.assertEqual(doc.read_text(encoding="utf-8"), "before\n\n<!-- job-status:start -->\nfresh\n<!-- job-status:end -->\n\nafter\n")
            self.assertFalse(list(tmp_path.glob("*.tmp")))

    def test_missing_ledger_degrades_to_unavailable(self) -> None:
        """账本不存在时报「未取得」，不抛异常。

        本脚本由收尾 hook 无人值守地跑，hook 的 traceback 没人盯着看；没有部署的机器
        上每次收尾都会走到这里，宁可数字缺位也不能崩或编数。"""
        with tempfile.TemporaryDirectory() as root:
            tmp_path = Path(root)
            block = MODULE.collect(tmp_path / "absent" / "ledger.db", tmp_path / "generated")
            text = "\n".join(block)
            self.assertIn("账本：未取得", text)
            self.assertIn(str(tmp_path / "absent" / "ledger.db"), text)
            self.assertIn("| `code-scrape.csv` | — | 未生成 | 番号刮削结果 |", text)
            self.assertNotIn("- 资产 ", text, "账本打不开时不得出现账本数字")
            self.assertTrue(text.endswith(MODULE.END), text[-60:])

    def test_ledger_without_schema_degrades_to_unavailable(self) -> None:
        """能连上但缺表的账本同样降级，而不是在查询处崩掉。"""
        with tempfile.TemporaryDirectory() as root:
            tmp_path = Path(root)
            db = tmp_path / "ledger.db"
            sqlite3.connect(db).close()
            block = MODULE.collect(db, tmp_path / "generated")
            text = "\n".join(block)
            self.assertIn("账本：未取得", text)
            self.assertNotIn("- 资产 ", text)

    def test_collect_reports_ledger_numbers_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            tmp_path = Path(root)
            db = tmp_path / "ledger.db"
            conn = sqlite3.connect(db)
            conn.executescript(
                "CREATE TABLE asset (id TEXT, medium TEXT, location TEXT, snapshot_path TEXT,"
                " duration REAL, code TEXT, studio TEXT);"
                "CREATE TABLE asset_tag (asset_id TEXT, source TEXT);"
                "INSERT INTO asset VALUES ('a1', 'video', 'local', NULL, 10.0, 'ABC-001', 'studio');"
            )
            conn.commit()
            conn.close()
            text = "\n".join(MODULE.collect(db, tmp_path / "generated"))
            self.assertIn("- 资产 1 条，其中视频 1 条。", text)
            self.assertIn("- 番号 1 个，其中 1 个有厂牌", text)
            self.assertIn("| `code-scrape.csv` | — | 未生成 | 番号刮削结果 |", text)
