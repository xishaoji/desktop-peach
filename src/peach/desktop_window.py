"""桌面窗口：用本机已装的 Chromium 系浏览器，以应用模式打开 Peach。

壳与网页版是同一份页面、同一个地址，区别只在窗口形态：`--app` 去掉地址栏与标签页，
看起来就是一个独立应用；profile 落在 Peach 数据目录下，与用户日常浏览器互不影响。
窗口左侧栏底部因此多一颗「在浏览器中打开」，点开后由服务端在默认浏览器里打开同一页。

不引 Electron、Tauri 或 WebView2（ADR-0065 已否决内嵌运行时）。用已装浏览器的那一份
好处是版本、渲染与解码能力都跟着系统走，坏处是这台机器可能一个都没装：那时回落到
默认浏览器，并告诉用户为什么。

登录仍走会话：窗口第一次打开时把一次性口令带在地址上（`?t=`），由
`routes_auth.query_auth_redirect` 换成 30 天的 `peach_session` cookie，紧接着 303
回到不带口令的地址。口令只在回环地址上发：局域网或隧道地址上的 `--app` 窗口没有
必要，也不该让口令出现在别的机器的历史里。

窗口身份同样不靠 UA 嗅探：`--app` 的 UA 与普通 Chromium 一模一样，改 UA 又会被页面
里按版本判断的逻辑读到。所以第一次打开时带上 `?desktop=1`，服务端在这个 profile 里
种一枚长期 cookie（`peach_desktop`），此后 `/healthz` 的 `desktop` 就为真。普通浏览器
没有这枚 cookie，同一份页面在那边不会多出那颗按钮。
"""
from __future__ import annotations

import logging
import subprocess
import sys
import webbrowser
from pathlib import Path, PurePath
from urllib.parse import urlsplit

from . import access, auth
from .browser_transport import find_browser

LOGGER = logging.getLogger(__name__)

#: 窗口 profile 落在 Peach 数据目录下：清理数据目录就是彻底清理窗口。
PROFILE_DIR = Path("state") / "desktop-profile"

#: 窗口尺寸。够放下桌面端的三栏布局，也不至于全屏盖住别的东西。
WINDOW_SIZE = (1440, 960)

#: 第一次打开时带的查询参数。服务端见到它就在这个 profile 里种 cookie，然后把它从
#: 地址上摘掉，地址栏里（窗口里没有地址栏，但历史记录与分享的链接里有）不留记号。
DESKTOP_QUERY = "desktop"
#: 窗口 profile 的记号。cookie 跟着 profile 走，翻到哪一页都还认得出。
DESKTOP_COOKIE = "peach_desktop"
#: 十年。窗口 profile 只由 Peach 打开，不存在「共享给别的浏览器」的问题。
DESKTOP_MAX_AGE = 10 * 365 * 86400

#: 关掉浏览器可能弹的首次运行引导与会话恢复。窗口恢复上一次那些标签页的话，
#: 托盘里点一次「打开 Peach」就会冒出好几页。
FLAGS = (
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-session-crashed-bubble",
    "--disable-features=Translate",
)

_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def profile_dir(config) -> Path:
    """窗口自己的浏览器 profile 目录。"""
    return Path(config.directory("state")) / PROFILE_DIR.name


def is_loopback(url: str) -> bool:
    """这个地址是不是本机回环。一次性口令只往这种地址上带。"""
    try:
        return (urlsplit(url).hostname or "") in _LOOPBACK
    except ValueError:
        return False


def _page_url(config, *, waiting: bool) -> str:
    """窗口与默认浏览器打开的是同一个地址：与托盘「打开 Peach」同一判据。"""
    from .tray import normal_url, setup_url

    return setup_url(config) if waiting else normal_url(config)


def _with_query(url: str, *pairs: tuple[str, str]) -> str:
    """在地址后面接参数，保留它原有的查询串。"""
    separator = "&" if urlsplit(url).query else "?"
    return url + separator + "&".join(f"{key}={value}" for key, value in pairs)


def window_url(config, *, waiting: bool = False) -> str:
    """窗口第一次打开的地址：普通那个地址加上窗口记号。"""
    return _with_query(_page_url(config, waiting=waiting), (DESKTOP_QUERY, "1"))


def browser_url(config, *, waiting: bool = False) -> str:
    """交给默认浏览器的地址：不带窗口记号，否则那颗按钮会跟着出现在那边。"""
    return _page_url(config, waiting=waiting)


def login_url(config, url: str) -> str:
    """需要一次性口令时把口令带上，返回窗口真正打开的地址。

    口令只从本机 secrets 目录读；读不到就照原地址开，服务会自己跳登录页。只有回环
    地址带口令：源码部署里正常地址是 `.local` 那个名字，那个地址上的窗口不替用户
    登录——口令出现在另一个主机名上没有任何好处。
    """
    if not is_loopback(url):
        return url
    policy = access.load(Path(config.directory("secrets")) / "access.json")
    if policy["mode"] == "open":
        # 开放访问模式没有口令可换：地址上多一个参数只会被原样留下。
        return url
    token = auth.read_token(Path(config.directory("secrets")))
    if not token:
        return url
    from urllib.parse import quote

    return _with_query(url, ("t", quote(token, safe="")))


def wants_window(request) -> bool:
    """这次请求是不是窗口第一次打开（地址上带着窗口记号）。"""
    return request.query_params.get(DESKTOP_QUERY) == "1"


def request_target(request) -> str:
    """这次请求的站内路径，查询串按原始编码带上。"""
    query = request.scope.get("query_string", b"").decode("latin-1")
    return (request.url.path or "/") + (f"?{query}" if query else "")


def strip_query(path: str, keys) -> str:
    """摘掉地址上的这几个参数，保留其余参数的原始编码；返回站内路径，交给 303 用。"""
    base, _, query = path.partition("?")
    pairs = [pair for pair in query.split("&")
             if pair and pair.split("=", 1)[0] not in keys]
    return base + (f"?{'&'.join(pairs)}" if pairs else "")


def set_desktop_cookie(response, request) -> bool:
    """给窗口的 profile 种长期记号；这次请求不是窗口第一次打开就什么都不做。"""
    if not wants_window(request):
        return False
    response.set_cookie(DESKTOP_COOKIE, "1", max_age=DESKTOP_MAX_AGE, path="/",
                        httponly=True, samesite="lax", secure=request.url.scheme == "https")
    return True


def is_desktop_request(cookie_value: str | None) -> bool:
    """这次请求是不是桌面窗口发出来的。

    判据只在服务端这一处：页面拿 `/healthz` 的 `desktop` 决定要不要露出「在浏览器中
    打开」，不在浏览器里自己嗅探 UA。用户手动种这枚 cookie 最多是多出一颗按了也只是
    开个浏览器的按钮，而那颗按钮的动作本身另有 `local_only` 闸门。
    """
    return cookie_value == "1"


def launch_arguments(executable: str | Path, config, url: str) -> list[str]:
    """浏览器命令行：应用模式、独立 profile、固定窗口大小与位置。"""
    return [
        str(executable),
        f"--app={url}",
        f"--user-data-dir={profile_dir(config)}",
        f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}",
        "--window-position=80,60",
        *FLAGS,
    ]


def open_window(config, *, waiting: bool = False, browser: str | None = None,
                popen=subprocess.Popen, open_browser=webbrowser.open) -> str:
    """打开桌面窗口，返回实际用的浏览器（`default` 表示回落到了默认浏览器）。

    `browser` 给测试用：不传就按 `find_browser()` 在真实机器上找。找不到浏览器时
    回落到默认浏览器打开同一个地址——比什么都不做强，只是没有独立窗口的形态。
    """
    executable = browser if browser is not None else find_browser()
    url = browser_url(config, waiting=waiting)
    if not executable:
        open_browser(login_url(config, url))
        LOGGER.info("未找到 Chrome 或 Edge，已在默认浏览器打开 %s", url)
        return "default"
    try:
        popen(launch_arguments(executable, config,
                               login_url(config, window_url(config, waiting=waiting))),
              close_fds=True, start_new_session=sys.platform != "win32")
    except OSError as error:
        LOGGER.warning("桌面窗口没能启动（%s），改用默认浏览器", error)
        open_browser(login_url(config, url))
        return "default"
    # 只记浏览器文件名，不记地址：地址上带着一次性口令。
    LOGGER.info("已用 %s 打开桌面窗口", PurePath(executable).name)
    return PurePath(executable).name


def open_in_default_browser(config, *, waiting: bool = False,
                            open_browser=webbrowser.open) -> str:
    """在默认浏览器里打开同一页：窗口左侧栏那颗「在浏览器中打开」的动作。

    这里不带一次性口令：用户在窗口里已经登录过，默认浏览器里那一份可能还是旧的
    会话。不带口令最多是多输一次密码，带上去则是把口令写进另一个浏览器的历史。
    """
    url = browser_url(config, waiting=waiting)
    open_browser(url)
    return url
