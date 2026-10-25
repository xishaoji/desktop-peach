<p align="center">
  <img src="resources/peach-logo.png" alt="Peach" width="96">
</p>

<h1 align="center">Peach</h1>

<p align="center">给自己用的私人影片馆藏：本地硬盘、网盘和关注的创作者，放在一个地方看。</p>

## 下载与使用

### Windows

1. 从 [Releases](https://github.com/xishaoji/desktop-peach/releases) 下载完整安装包 `Peach-<版本>-windows-x64-setup.exe`，双击安装，无需管理员权限；也可下载完整免安装包，解压后运行 `Peach.exe`。
2. 首次设置页会自己打开：装了 Chrome 或 Edge 时是独立的桌面窗口，没有时是默认浏览器。选择媒体目录和访问范围。
3. 开始扫描，随后在桌面窗口或浏览器里浏览和播放；窗口左侧栏底部可以随时回到浏览器。

完整安装包携带媒体工具，可离线安装；轻量安装包体积更小，安装时按需下载工具。

测试版尚未签名。若 Windows 提示「已保护你的电脑」，确认来自本项目的 Release，再点「更多信息 → 仍要运行」。安装、更新和故障排查见 [Windows 使用说明](docs/TESTING_DESKTOP.md)。

### macOS 本机构建

macOS 可在本机构建独立 `.app` 与 DMG，运行不需要 Python、源码或另一台电脑。构建与安装见 [macOS 独立测试包](docs/OPERATIONS.md#macos-独立测试包)。

发版流程分别生成 Apple Silicon（`macos-arm64.dmg`）与 Intel（`macos-x86_64.dmg`）测试包；可下载版本以 [Releases](https://github.com/xishaoji/desktop-peach/releases) 的实际资产为准。在「关于本机」确认芯片，打开对应镜像并将 Peach 拖入「应用程序」。测试包未完成 Apple 公证，媒体工具需另行准备。

### 从源码运行（Windows、macOS）

需要 Git、uv 和 Python 3.14 或更高，步骤见[源码运行](docs/OPERATIONS.md#从源码运行windowsmacos)。

### Docker（VPS / NAS）

部署步骤与支持范围见 [Docker 部署](docs/DOCKER.md)。

## 常见问题

- **会改我的文件吗？** 扫描、补资料不碰原文件。手动整理会改名或移动；清空回收站会删除文件，资源同步中的清理会删除空目录，均需确认。
- **支持哪些网盘？** 通过 CloudDrive2 等工具挂载为本地目录的网盘，详见[网盘挂载](docs/CLOUDDRIVE.md)；云下载支持 115 和 PikPak，登录令牌保存在本机。
- **手机上怎么看？** 首次设置时允许同一局域网的设备访问，手机连同一网络，打开设置页给出的地址，并设置访问密码。
- **数据存在哪？** 本机的 `peach-data` 文件夹；Windows 版默认位于 `%LOCALAPPDATA%\Peach\peach-data`。

## 问题反馈

先查看[用户反馈](https://github.com/xishaoji/desktop-peach/issues)，或[提交问题](https://github.com/xishaoji/desktop-peach/issues/new/choose)，附上版本、操作步骤、预期与实际结果。截图请遮掉凭据、局域网地址和完整路径，不要上传数据库或媒体；安全问题按[安全政策](SECURITY.md)私下报告。

## 文档

[运行与配置](docs/OPERATIONS.md) · [来源采集](docs/SOURCING.md) · [外部播放器](docs/EXTERNAL_PLAYERS.md) · [变更日志](CHANGELOG.md)

参与开发：[开发约定](AGENTS.md) · [测试与依赖](docs/TESTING.md)

## 相关项目

感谢 [amane](https://github.com/sqzw-x/amane)、[Gfriends](https://github.com/gfriends/gfriends)、[CloudDrive2](https://www.clouddrive2.com/) 等项目，完整致谢与复用说明见[复用清单](docs/REUSE.md)。

## Star 历史

<a href="https://www.star-history.com/?repos=xishaoji%2Fdesktop-peach&amp;type=date&amp;logscale=&amp;releases=&amp;legend=top-left">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=xishaoji/desktop-peach&amp;type=date&amp;theme=dark&amp;logscale&amp;legend=top-left">
    <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=xishaoji/desktop-peach&amp;type=date&amp;logscale&amp;legend=top-left">
    <img alt="Peach Star 历史" src="https://api.star-history.com/svg?repos=xishaoji/desktop-peach&amp;type=date&amp;logscale&amp;legend=top-left">
  </picture>
</a>

## 许可证

[AGPL-3.0-or-later](LICENSE) · Copyright (C) 2026 longmeidao。第三方组件保留各自的许可证。
