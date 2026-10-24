# ADR-0114：桌面窗口由已装 Chromium 的应用模式承载

- 状态：已采纳
- 日期：2026-10-24
- 相关：ADR-0065、ADR-0031

## 背景

Peach 原先只能从浏览器打开。要一个「桌面客户端」时，有三条路：打包一个内嵌运行时（Electron、
Tauri、WebView2），用系统自带浏览器以应用模式开窗（`--app`），或维持浏览器标签页。

ADR-0065 已经为采集侧否决过 WebView2：多 15 MB 依赖、只在 Windows 上有，且它驱动的浏览器版本
与系统其余部分不一致。桌面窗口多出来的需求只有两件：没有地址栏的独立窗口，以及一个回到默认
浏览器的入口。这两件事都不需要第二个运行时，也不需要 Peach 自己渲染页面。

## 决策

**一、窗口用本机已装的 Chromium 系浏览器以应用模式启动。** `peach.desktop_window.open_window`
复用 `browser_transport.find_browser()`（Chrome 优先、Edge 次之、尊重 `PEACH_BROWSER`），以
`--app=<地址> --user-data-dir=<数据根>/state/desktop-profile` 拉起。页面与网页版是同一份构建、
同一个地址，区别只在窗口形态与那份独立 profile。不新增运行时依赖，也就不新增打包、签名与
更新路径。

**二、窗口身份由服务端下发，不嗅探 UA。** `--app` 的 UA 与普通 Chromium 完全相同，改 UA 会让页面
里按版本判断的逻辑读错。第一次开窗的地址带 `desktop=1`，`routes_pages.index` 在同一个 303 里种下
长期 cookie `peach_desktop`，此后 `is_desktop_request` 是唯一的判据，`/healthz` 的 `desktop` 字段也
取自它。普通浏览器没有这枚 cookie，同一份页面在那边不会多出「在浏览器中打开」。

**三、登录沿现有一次性口令。** 窗口第一次打开时把 `?t=<口令>` 带在地址上，交给
`routes_auth.query_auth_redirect` 与 `set_auth_cookie` 换成会话 cookie，然后 303 摘掉。口令只发往
回环地址：局域网与隧道地址上的窗口没有必要，也不该让口令进入别的机器的历史记录。地址上摘记号
只摘与本次口令相等的那几个 `t`，播放时间 `t=42` 原样保留。

**四、回到浏览器由服务端执行。** 窗口里没有 `window.open` 到默认浏览器的可靠通道，所以左侧栏的
「在浏览器中打开」走 `POST /api/open-in-browser`，服务端用 `webbrowser.open` 打开同一地址。端点只接受
本机连接，判据与配置写入相同。托盘菜单把「打开 Peach」指向窗口、「在浏览器中打开」指向这个动作，
两处是同一件事的两个入口。

**五、找不到浏览器就回落，并说明原因。** 没有 Chrome 或 Edge 时用 `webbrowser.open` 打开默认浏览器，
托盘给出提示；回落路径不带一次性口令，默认浏览器里该登录就登录。

## 后果

窗口的渲染、编解码与媒体能力跟着系统浏览器走：浏览器升级即生效，Peach 不用跟着发版。代价是
这台机器必须装一个 Chromium 系浏览器，没有时只能回落；profile 独立意味着窗口里没有用户日常
浏览器的登录态与扩展，这正是要的，但也意味着窗口自己那份 cookie 需要清理时只能删数据目录下的
`state/desktop-profile`。

播放器不变。窗口与普通页同源，Video.js 的 Range、HLS 与转码、ISO 交给外部播放器的分支都照常。
