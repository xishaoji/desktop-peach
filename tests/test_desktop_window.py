"""桌面窗口：窗口形态、一次性登录与「这是窗口吗」的判据。

`--app` 窗口与网页版是同一份页面、同一个地址，这里要验的是两边真的只在窗口形态与那
一颗按钮上有区别：地址只在本机回环上带一次性口令、窗口记号只种一次、普通浏览器拿不到
窗口身份、找不到浏览器时回落默认浏览器。
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from peach import access, auth, desktop_window, settings_file


class Config:
    """设置对象里窗口用得上的那几件：数据目录、端口、mDNS 名。"""

    def __init__(self, root: Path):
        self.data_root = root
        self.server = settings_file.ServerSettings()

    def directory(self, key: str) -> Path:
        return self.data_root / key


class DesktopWindowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.joinpath("state").mkdir()
        self.config = Config(self.root)

    def secrets(self) -> Path:
        path = self.config.directory("secrets")
        path.mkdir()
        auth.ensure_token(path)
        return path

    def test_the_window_gets_its_own_profile_under_the_data_root(self):
        self.assertEqual(desktop_window.profile_dir(self.config),
                         self.root / "state" / "desktop-profile")

    def test_launch_arguments_open_an_app_window_with_that_profile(self):
        url = "https://peach.local/?desktop=1"
        arguments = desktop_window.launch_arguments(r"E:\chrome\chrome.exe", self.config, url)
        self.assertEqual(arguments[0], r"E:\chrome\chrome.exe")
        self.assertIn(f"--app={url}", arguments)
        self.assertIn(f"--user-data-dir={self.root / 'state' / 'desktop-profile'}", arguments)
        self.assertIn("--window-size=1440,960", arguments)
        # 自己的 profile 才是「窗口」：与日常浏览器共用 profile 会恢复出一排标签页。
        self.assertIn("--no-first-run", arguments)
        self.assertNotIn("--headless", arguments)

    def test_the_window_url_carries_the_marker_and_the_browser_url_does_not(self):
        with mock.patch("peach.distribution.standalone", return_value=False):
            window = desktop_window.window_url(self.config)
            browser = desktop_window.browser_url(self.config)
        self.assertTrue(window.endswith("/?desktop=1"), window)
        self.assertEqual(browser, "https://peach.local/")

    def test_a_loopback_window_url_carries_the_one_time_token(self):
        token = auth.read_token(self.secrets())
        # 独立包从回环地址打开：源码部署正常地址是 `.local`，那一份不带口令。
        with mock.patch("peach.distribution.standalone", return_value=True):
            url = desktop_window.window_url(self.config)
        self.assertTrue(url.startswith("http://127.0.0.1"), url)
        with mock.patch.object(access, "load", return_value={"mode": "legacy", "revision": "legacy"}):
            signed = desktop_window.login_url(self.config, url)
        self.assertIn(f"t={token}", signed)
        self.assertIn("desktop=1", signed)

    def test_a_lan_address_never_gets_the_token(self):
        """口令只在本机回环上发：`.local` 那个名字上的窗口不替人登录。"""
        self.secrets()
        url = "https://peach.local/?desktop=1"
        with mock.patch.object(access, "load", return_value={"mode": "legacy", "revision": "legacy"}):
            self.assertEqual(desktop_window.login_url(self.config, url), url)

    def test_open_access_mode_adds_nothing_to_the_address(self):
        self.secrets()
        url = desktop_window.window_url(self.config)
        with mock.patch.object(access, "load", return_value={"mode": "open", "revision": "r"}):
            self.assertEqual(desktop_window.login_url(self.config, url), url)

    def test_a_missing_browser_falls_back_to_the_default_one(self):
        opened = []
        name = desktop_window.open_window(self.config, browser="",
                                          popen=mock.Mock(), open_browser=opened.append)
        self.assertEqual(name, "default")
        self.assertEqual(len(opened), 1, "回落时要在默认浏览器里打开同一页")
        self.assertNotIn("t=", opened[0], "默认浏览器里那份会话不该被窗口的登录替换掉")

    def test_a_failing_browser_also_falls_back(self):
        opened = []
        with mock.patch.object(desktop_window, "find_browser", return_value=r"E:\chrome\chrome.exe"):
            name = desktop_window.open_window(
                self.config, popen=mock.Mock(side_effect=OSError("no such file")),
                open_browser=opened.append)
        self.assertEqual(name, "default")
        self.assertEqual(len(opened), 1)

    def test_the_window_is_started_detached_and_named_in_the_log_not_the_url(self):
        popen = mock.Mock()
        with mock.patch.object(desktop_window, "find_browser", return_value=r"E:\chrome\chrome.exe"), \
                mock.patch.object(desktop_window.LOGGER, "info") as logged:
            name = desktop_window.open_window(self.config, popen=popen)
        self.assertEqual(name, "chrome.exe")
        _, kwargs = popen.call_args
        self.assertTrue(kwargs["close_fds"])
        # Windows 上 `start_new_session` 没有意义，别把浏览器的控制台挂到托盘进程上。
        self.assertEqual(kwargs["start_new_session"], desktop_window.sys.platform != "win32")
        self.assertEqual(logged.call_args.args, ("已用 %s 打开桌面窗口", "chrome.exe"))

    def test_a_posix_path_browser_is_also_named_by_its_filename(self):
        """macOS 上 find_browser 返回 POSIX 路径；名字取法和 Windows 路径一致。"""
        popen = mock.Mock()
        posix_exe = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        with mock.patch.object(desktop_window, "find_browser", return_value=posix_exe), \
                mock.patch.object(desktop_window.LOGGER, "info") as logged:
            name = desktop_window.open_window(self.config, popen=popen)
        self.assertEqual(name, "Google Chrome")
        self.assertEqual(logged.call_args.args, ("已用 %s 打开桌面窗口", "Google Chrome"))

    def test_the_default_browser_entry_opens_the_plain_address(self):
        opened = []
        with mock.patch("peach.distribution.standalone", return_value=False):
            url = desktop_window.open_in_default_browser(self.config, open_browser=opened.append)
        self.assertEqual(url, "https://peach.local/")
        self.assertEqual(opened, [url])
        self.assertNotIn("desktop=1", url, "网页版不该带窗口记号")

    def test_the_window_marker_is_stripped_but_playback_time_survives(self):
        self.assertEqual(desktop_window.strip_query("/assets/1?tag=a%20b&desktop=1&t=42&tag=c",
                                                    {"desktop"}),
                         "/assets/1?tag=a%20b&t=42&tag=c")
        self.assertEqual(desktop_window.strip_query("/?desktop=1", {"desktop"}), "/")

    def test_is_loopback_recognises_the_three_forms(self):
        for url in ("http://127.0.0.1:8900/", "https://localhost/", "http://[::1]/"):
            with self.subTest(url=url):
                self.assertTrue(desktop_window.is_loopback(url))
        for url in ("https://peach.local/", "https://192.0.2.10/", "not a url"):
            with self.subTest(url=url):
                self.assertFalse(desktop_window.is_loopback(url))

    def test_only_the_cookie_marks_a_desktop_request(self):
        self.assertTrue(desktop_window.is_desktop_request("1"))
        for value in (None, "", "0", "true"):
            with self.subTest(value=value):
                self.assertFalse(desktop_window.is_desktop_request(value))


class _Response:
    def __init__(self):
        self.cookies = {}

    def set_cookie(self, key, value, **options):
        self.cookies[key] = (value, options)


class _Request:
    def __init__(self, query="", scheme="https"):
        self.query_params = dict(pair.split("=", 1) for pair in query.split("&") if pair)
        self.scope = {"query_string": query.encode("latin-1")}
        self.url = mock.Mock(path="/", scheme=scheme)
        self.url.path = "/"


class DesktopCookieTests(unittest.TestCase):
    def test_the_window_writes_one_long_lived_http_only_cookie(self):
        response, request = _Response(), _Request("desktop=1")
        self.assertTrue(desktop_window.set_desktop_cookie(response, request))
        value, options = response.cookies[desktop_window.DESKTOP_COOKIE]
        self.assertEqual(value, "1")
        self.assertEqual(options["max_age"], desktop_window.DESKTOP_MAX_AGE)
        self.assertTrue(options["httponly"])
        self.assertEqual(options["samesite"], "lax")
        self.assertTrue(options["secure"], "HTTPS 部署上这枚 cookie 也只能走 TLS")
        self.assertEqual(options["path"], "/")

    def test_a_plain_page_visit_writes_nothing(self):
        response = _Response()
        self.assertFalse(desktop_window.set_desktop_cookie(response, _Request("")))
        self.assertEqual(response.cookies, {})

    def test_wants_window_only_answers_the_exact_marker(self):
        self.assertTrue(desktop_window.wants_window(_Request("desktop=1")))
        for query in ("", "desktop=0", "desktop=", "x=desktop"):
            with self.subTest(query=query):
                self.assertFalse(desktop_window.wants_window(_Request(query)))


if __name__ == "__main__":
    unittest.main()
