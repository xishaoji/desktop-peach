"""待处理视频：把扫描与修复留下的几处「这条要人看一眼」收进同一张清单。

问题的来源本来就分散——缺时长的判据在 `library_diagnostics`，播不动的结论在
`web_media_repair` 的扫描缓存里，扫描与采集的失败项在 `issues.jsonl` 里。分散本身
不是错：每一处都跟着自己那一轮任务的生命周期。错的是它们都只给读数、不给动作，
于是页面提示「有问题需要处理」，点进去却没有地方处理。

这里只做两件事：

- **投影**：把三处判据读成同一行形状，带上这条能不能自动重试（`reprobe`）与不能的
  原因（`blocked_reason`）。投影不改任何状态。
- **动作**：忽略、恢复、重新探测、移入回收站、重试采集。移入回收站转交
  `web_batch.w_batch`，自己绝不写 `asset.disposal`：回收与撤销只有那一条实现。

忽略落在 `review_decision`（`category='pending_video'`），只表示「不要再提示这一条」，
不改馆藏行。键是 `<kind>:<asset_id>`，与垃圾队列 `junk_file` 那一套同源：删掉这个键
就是恢复。「这一条我看过了」是人对那一部片说的话，所以按资产记，不按文件名。

重新探测只认 `duration IS NULL` 那一类：`media_probe.probe_unmeasured` 的入库队列
就是这条判据。已经探过、拿到 `-1` 的行不在那条队列里，硬把它塞进去等于给探测脚本
开第二份口径；这一类标成不可重试并说明该走哪条路。计费来源（PikPak）上重探等于把
整片拉一遍，属于用户显式同意才做的事，这一页不代劳。
"""
from __future__ import annotations

import json
import time
from pathlib import PureWindowsPath

from . import media_libraries, media_probe, media_sources, settings_file, strm
from .ffmpeg import FFmpegResolver
from .library_processing import NOTE_KEYS, issues_path, snapshot
from .web_batch import w_batch
from .web_media_repair import CLEAN, HEADER, INDEX, UNAVAILABLE, _load_cache

#: 一条记录的问题类型。顺序就是页面上的顺序：先缺东西的，再播不动的，最后是扫描与
#: 采集没做成的。
KINDS = ("duration", "media", "processing")

KIND_LABELS = {
    "duration": "缺时长",
    "media": "播放异常",
    "processing": "采集未完成",
}

#: 这三种结论都算「播不动」；`clean` 不进清单。
MEDIA_VERDICTS = (HEADER, INDEX, UNAVAILABLE)

MEDIA_DETAIL = {
    HEADER: "缺时间戳表，浏览器播放会丢帧",
    INDEX: "缺文件索引，打不开",
    UNAVAILABLE: "上一次修复没有成功",
}

#: 忽略集合的类别。`review_decision` 的 `item_key` 是 `<kind>:<asset_id>`。
IGNORE_CATEGORY = "pending_video"

#: 一次动作最多接多少个。移入回收站与重新探测都是串行做的，接太多只是在后台排一条
#: 长队；剩下的处理完再点一次。
ACTION_LIMIT = 200

#: 采集问题日志最多读多少行。诊断报告遇到超大日志整份放弃；这里只取前若干行，
#: 剩下的仍能在日志原文里看。
MAX_ISSUE_LINES = 5000


def _ignore_key(kind: str, asset_id: int) -> str:
    return f"{kind}:{asset_id}"


def _ignored(contract) -> set[str]:
    with contract.read_connection() as connection:
        return {row[0] for row in connection.execute(
            "SELECT item_key FROM review_decision WHERE category=?", (IGNORE_CATEGORY,))}


def _library_resolver(config):
    """把 `(location, path)` 映到媒体库名；来源没有配根时是空串。

    每页一次建立，逐行查一张前缀表。按行去问 `media_libraries.libraries` 的话，
    几千行就是几千次同样的目录遍历。
    """
    declared = []
    for library in media_libraries.libraries(config):
        for root in library["roots"]:
            declared.append((root["location"], str(PureWindowsPath(root["root"])).rstrip("\\") + "\\",
                             library["id"]))
    declared.sort(key=lambda row: len(row[1]), reverse=True)

    def resolve(location: str, path: str | None) -> str:
        text = str(path or "").upper()
        return next((name for place, prefix, name in declared
                     if place == location and text.startswith(prefix.upper())), "")

    return resolve


def _identity(row) -> tuple[str, str, str]:
    """番号、标题、文件名。没有标题时用文件名：路径是去磁盘上确认或改名时真正要用的
    东西，所以它单独留在 `path` 里。"""
    name = PureWindowsPath(str(row["path"] or "")).name or str(row["name"] or "")
    return str(row["code"] or ""), str(row["catalog_title"] or "") or name, name


def _row(asset_id: int, kind: str, row, library, *, detail: str, reprobe: bool,
         blocked_reason: str = "", **extra) -> dict:
    code, title, name = _identity(row)
    return {
        "id": asset_id, "kind": kind, "code": code, "title": title, "name": name,
        "path": str(row["path"] or ""), "location": row["location"],
        "library": library(row["location"], row["path"]),
        "detail": detail, "reprobe": reprobe, "blocked_reason": blocked_reason,
        **extra,
    }


def _duration_rows(contract, config, library) -> list[dict]:
    """缺时长：与诊断报告同一判据（`library_diagnostics.library` 的 duration 组）。

    回收站与已消失的行不算：那些片子本来就不在库上，缺不缺时长不是待办。
    """
    with contract.read_connection() as connection:
        rows = connection.execute(
            "SELECT id,code,catalog_title,path,name,location,duration FROM asset "
            "WHERE medium='video' AND COALESCE(disposal,'') NOT IN ('vanished','trash') "
            "AND (duration IS NULL OR duration<=0) ORDER BY id").fetchall()
    items = []
    for row in rows:
        raw = str(row["path"] or "")
        pointer = strm.skip_background_read(raw)
        metered = media_sources.cost_for(row["location"], raw, config) == "metered"
        if row["duration"] is not None:
            detail, reprobe = "探测过但没有拿到时长", False
            blocked = "这一条已经探过；重探请用 scripts/probe.py --redo failed"
        elif pointer:
            detail, reprobe = "还没有探到时长", False
            blocked = "STRM 与 ISO 不执行视频探测"
        elif metered:
            detail, reprobe = "还没有探到时长", False
            blocked = "这个来源按流量计费，重探要整片读一遍，请在探测脚本里显式允许"
        else:
            detail, reprobe, blocked = "还没有探到时长", True, ""
        items.append(_row(row["id"], "duration", row, library, detail=detail,
                          reprobe=reprobe, blocked_reason=blocked))
    return items


def _media_rows(contract, config, library) -> list[dict]:
    """播放异常：媒体修复那一轮写下的判定缓存。

    缓存键是「资产 + 大小 + 改动时间」。这里只用账本里的大小对一遍：文件换了大小，
    旧结论显然不作数；改了内容而大小不变的，下一轮修复会自己重判。每条都去 stat
    一遍文件不行——这一页的判据不能连累网盘做几千次网络往返。
    """
    entries = _load_cache(contract)
    if not entries:
        return []
    current: dict[str, str] = {}
    for key, verdict in entries.items():
        if verdict != CLEAN:
            current[key.split("-", 1)[0]] = key
    with contract.read_connection() as connection:
        rows = connection.execute(
            "SELECT id,code,catalog_title,path,name,location,size FROM asset "
            "WHERE medium='video' AND disposal IS NULL ORDER BY id").fetchall()
    items = []
    for row in rows:
        key = current.get(str(row["id"]))
        if key is None or not _size_matches(key, row):
            continue
        verdict = entries[key]
        items.append(_row(row["id"], "media", row, library, detail=MEDIA_DETAIL.get(verdict, verdict),
                          reprobe=False,
                          blocked_reason="修复在数据管理页的「媒体修复」里按媒体库整轮执行"))
    return items


def _size_matches(key: str, row) -> bool:
    """缓存键里的文件大小还作数吗。"""
    parts = key.split("-")
    if len(parts) != 3 or row["size"] is None:
        return False
    try:
        return int(parts[1]) == int(row["size"])
    except ValueError:
        return False


def _processing_rows(contract, config, library) -> list[dict]:
    """采集未完成：最近那一趟扫描与采集写下的问题日志。

    只读日志、不读任务进度：状态文件会被下一趟覆盖，日志按任务各一份，是唯一还能
    逐条看的证据。告知项（「来源没有这部片」）不算问题，与 `_record_issue` 同一口径。
    """
    state = snapshot(config)
    job_id = str(state.get("job_id") or "")
    if not job_id:
        return []
    path = issues_path(config, job_id)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[:MAX_ISSUE_LINES]
    except OSError:
        return []
    retryable = {int(value) for value in state.get("retryable_asset_ids") or []}
    records: dict[int, str] = {}
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("message") in NOTE_KEYS or record.get("severity") == "info":
            continue
        try:
            asset_id = int(record.get("asset_id"))
        except (TypeError, ValueError):
            continue
        records.setdefault(asset_id, str(record.get("message") or "处理未完成"))
    assets = _asset_rows(contract, records)
    items = []
    for asset_id, detail in records.items():
        found = assets.get(asset_id)
        if found is None:
            continue
        retry = asset_id in retryable
        items.append(_row(asset_id, "processing", found, library, detail=detail,
                          reprobe=False, blocked_reason="" if retry else "这一项不在上一轮的可重试集合里",
                          retryable=retry, job_id=job_id))
    return items


def _asset_rows(contract, asset_ids) -> dict[int, object]:
    """一次取回这批资产：问题日志最多几千行，逐行各查一次等于几千次同样的往返。

    按 500 个一批切：SQLite 对一条语句里的参数个数有上限，日志读满几千行时不能
    把它们全塞进同一个 `IN`。
    """
    ids = list(asset_ids)
    found: dict[int, object] = {}
    with contract.read_connection() as connection:
        for offset in range(0, len(ids), 500):
            chunk = ids[offset:offset + 500]
            rows = connection.execute(
                "SELECT id,code,catalog_title,path,name,location FROM asset WHERE id IN ("
                + ",".join("?" * len(chunk)) + ")", chunk).fetchall()
            found.update({int(row["id"]): row for row in rows})
    return found


def _reprobe_state(contract) -> dict:
    state = contract.pending_reprobe_job.snapshot()
    if state is None:
        return {"status": "idle", "job_id": "", "checked": 0, "total": 0, "message": ""}
    return {**{key: state.get(key) for key in ("job_id", "status", "checked", "total", "message", "error")},
            "stage": state.get("stage", "")}


def q_pending_videos(contract, args=None):
    """待处理视频清单；`kind` 过滤某一类，`include_ignored=1` 把已忽略的也列出来。

    `counts=1` 只回读数：数据管理页那张卡片与侧栏只要几个数字，整页那份明细带着每条
    的路径，几千行没有理由为了一张卡搬一遍。
    """
    args = args or {}
    config = settings_file.active()
    want = str(args.get("kind") or "").strip()
    if want and want not in KINDS:
        raise ValueError("没有这一类问题")
    include_ignored = str(args.get("include_ignored") or "") in {"1", "true"}
    counts_only = str(args.get("counts") or "") in {"1", "true"}
    ignored = _ignored(contract)
    library = _library_resolver(config)
    items = []
    for kind, rows in (("duration", _duration_rows), ("media", _media_rows),
                       ("processing", _processing_rows)):
        for item in rows(contract, config, library):
            item["ignored"] = _ignore_key(kind, item["id"]) in ignored
            items.append(item)
    visible = [item for item in items if include_ignored or not item["ignored"]]
    if want:
        visible = [item for item in visible if item["kind"] == want]
    counts = {kind: {"total": 0, "reprobe": 0, "blocked": 0, "ignored": 0} for kind in KINDS}
    for item in items:
        bucket = counts[item["kind"]]
        bucket["total"] += 1
        bucket["ignored" if item["ignored"] else "reprobe" if item["reprobe"] else "blocked"] += 1
    # 「可重新探测 / 需另走一步」只算还没被忽略的那些：被忽略的不算待办，页脚的
    # `ignored_total` 单独讲。`include_ignored=1` 时两者之和因此小于 `total`。
    reprobe = sum(1 for item in visible if item["reprobe"] and not item["ignored"])
    blocked = sum(1 for item in visible if not item["ignored"]) - reprobe
    payload = {
        "items": [] if counts_only else visible,
        "counts": counts,
        "kinds": [{"kind": kind, "label": KIND_LABELS[kind], **counts[kind]} for kind in KINDS],
        "kind": want,
        "include_ignored": include_ignored,
        "total": len(visible),
        "reprobe": reprobe,
        "blocked": blocked,
        "ignored_total": sum(1 for item in items if item["ignored"]),
    }
    if not counts_only:
        payload["job"] = _reprobe_state(contract)
    return payload


def _ids(body) -> list[int]:
    raw = body.get("ids")
    if not isinstance(raw, list):
        raise TypeError("ids 必须是列表")
    ids = list(dict.fromkeys(int(item) for item in raw))
    if not ids or len(ids) > ACTION_LIMIT:
        raise ValueError(f"一次需要 1 到 {ACTION_LIMIT} 条")
    return ids


def _kinds(body) -> list[str]:
    kinds = body.get("kinds")
    if isinstance(kinds, str):
        kinds = [kinds]
    if not isinstance(kinds, list) or not kinds or any(kind not in KINDS for kind in kinds):
        raise ValueError("问题类型不正确")
    return list(dict.fromkeys(kinds))


def _write_ignore(contract, kind: str, asset_ids) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with contract.write_transaction() as connection:
        connection.executemany(
            "INSERT INTO review_decision(category,item_key,status,note,updated_at) "
            "VALUES(?,?,'skipped','用户忽略了这条待处理提示',?) "
            "ON CONFLICT(category,item_key) DO UPDATE SET "
            "status='skipped',note=excluded.note,updated_at=excluded.updated_at",
            [(IGNORE_CATEGORY, _ignore_key(kind, asset_id), stamp) for asset_id in asset_ids])


def _clear_ignore(contract, kind: str, asset_ids) -> None:
    with contract.write_transaction() as connection:
        connection.executemany(
            "DELETE FROM review_decision WHERE category=? AND item_key=?",
            [(IGNORE_CATEGORY, _ignore_key(kind, asset_id)) for asset_id in asset_ids])


def _reprobe_scope(contract, config, asset_ids) -> dict[tuple[str, str], list[int]]:
    """要重探的 id 按「来源 + 根」归组：`probe_unmeasured` 一次只认一个根。

    只收这一页能重探的那些：没探过（`duration IS NULL`）、不是 STRM/ISO、来源的读取
    不计费。其余静默略过，调用方按空集报「没有可重新探测的」。
    """
    marks = ",".join("?" * len(asset_ids))
    with contract.read_connection() as connection:
        rows = connection.execute(
            "SELECT id,location,path,medium,duration FROM asset WHERE id IN (" + marks + ")",
            list(asset_ids)).fetchall()
    groups: dict[tuple[str, str], list[int]] = {}
    for row in rows:
        raw = str(row["path"] or "")
        if (row["medium"] != "video" or row["duration"] is not None or not raw
                or strm.skip_background_read(raw)
                or media_sources.cost_for(row["location"], raw, config) != "free"):
            continue
        root = _root_of(config, row["location"], raw)
        if root is not None:
            groups.setdefault((row["location"], root), []).append(row["id"])
    return groups


def _root_of(config, location: str, path: str) -> str | None:
    """最长的那个已声明根：同一来源下嵌套根时，归到更具体的那个。"""
    best = ""
    for root in config.locations.get(location, ()):
        prefix = str(PureWindowsPath(root)).rstrip("\\")
        if str(path).upper().startswith((prefix + "\\").upper()) and len(prefix) > len(best):
            best = prefix
    return best or None


def _run_reprobe(contract, config, job_id: str, scope) -> None:
    job = contract.pending_reprobe_job
    choice = FFmpegResolver(config.directory("tools") / "ffmpeg").ffprobe()
    if choice is None:
        job.update(job_id, status="failed", error="这个部署没有 ffprobe，探测没有执行",
                   completed_at=time.time())
        return
    done = 0
    try:
        for (location, root), ids in scope.items():
            if job.snapshot() is None:
                return
            job.update(job_id, stage="读取时长与分辨率", message=root)
            done += media_probe.probe_unmeasured(
                contract.db_path, str(choice.path), location, root, asset_ids=ids, config=config,
                report=lambda checked, total, base=done: job.update(
                    job_id, checked=base + checked, total=base + total))
        job.update(job_id, status="complete", checked=done, total=done,
                   message="探测结束", completed_at=time.time())
    except Exception as error:  # noqa: BLE001  后台失败与其他任务同一写法：变成可轮询的状态
        job.update(job_id, status="failed", error=f"{type(error).__name__}: {error}",
                   completed_at=time.time())
    finally:
        contract.cache_bust()


def _reprobe(contract, config, body):
    asset_ids = _ids(body)
    scope = _reprobe_scope(contract, config, asset_ids)
    if not scope:
        raise ValueError("选中的视频都不在可重新探测的范围里")
    return contract.pending_reprobe_job.start(
        lambda job_id: _run_reprobe(contract, config, job_id, scope),
        initial={"stage": "准备探测", "checked": 0, "total": 0, "message": ""},
        restart=True, recovery={"action": "reprobe",
                                "ids": [asset_id for ids in scope.values() for asset_id in ids]})


def _retry_processing(contract, body):
    """把采集的失败项交回扫描与采集重试；任务本体与互斥仍归那一轮任务管。"""
    from .web_library_processing import w_library_processing
    asset_ids = _ids(body)
    job_id = str(body.get("job_id") or "")
    if not job_id:
        raise ValueError("请先刷新页面，缺了原任务的编号")
    return w_library_processing(contract, {"job_id": job_id, "retry": asset_ids})


def w_pending_action(contract, body=None):
    """忽略、恢复、重新探测、移入回收站、重试采集。五个动作各自只做一件事。"""
    body = body or {}
    action = body.get("action")
    if action in {"ignore", "unignore"}:
        asset_ids = _ids(body)
        kinds = _kinds(body)
        write = _write_ignore if action == "ignore" else _clear_ignore
        for kind in kinds:
            write(contract, kind, asset_ids)
        contract.cache_bust()
        return {"ok": True, "action": action, "changed": len(asset_ids) * len(kinds)}
    if action == "reprobe":
        return {"ok": True, "action": action, **_reprobe(contract, settings_file.active(), body)}
    if action == "retry":
        return {"ok": True, "action": action, "job": _retry_processing(contract, body)}
    if action == "dispose":
        ids = _ids(body)
        return {"ok": True, "action": action,
                "result": w_batch(contract, {"ids": ids, "operation": "dispose",
                                             "disposal_reason": "manual"})}
    raise ValueError("不需要处理的动作")
