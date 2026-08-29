# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与语义化版本。

## [1.4.0] - 2026-08-30

### Added
- **后台任务段**:当前会话有 running 的后台任务/子 agent 时,状态栏 swarm 标记后显示可点击的 `⚙ N`(OSC 8 超链接;上游源码逐行确认渲染链零剥离、宽度零占、截断自动补关闭符、全屏模式点击走 openUrl);点击在浏览器打开本地任务看板(状态/描述/耗时 + output.log 链接,2s 自动刷新,内容不变零重写),与 `/tasks` 面板同源(会话 `tasks/*.json`);无 running 时段隐藏不占位;swarm 动效通道同步剥除 OSC 8 防水波串乱;回归 +5(总 59)

## [1.3.3] - 2026-08-28

### Fixed
- 切账号(logout/login)后 5h/7d 额度仍显示旧账号:CLI 0.38.0 双 OAuth(kimi.com/kimi.ai)把凭据改为按环境分槽位(`kimi-code-env-<hash>.json`),国际版端点在 `api.kimi.ai`,而脚本写死老凭据文件与 `api.kimi.com`,国际版登录后拉取失败、一直显示缓存里旧账号额度;现凭据槽位与端点跟随 `config.toml` 的 `[providers."managed:kimi-code"]`(`oauth.key` 定凭据、`base_url` 定端点,与 CLI 源码 `resolveKimiCodeRuntimeAuth` 同口径),读不到配置回退国内默认槽位;回归 +4(总 54)

## [1.3.2] - 2026-08-16

### Fixed
- 多窗口/多会话同开时 token/金额/TPS 段周期性消失:缓存单 `sess` 槽位被另一会话的后台刷新互相覆盖;会话条目改映射存储(按 sid 各写各的,按最近活跃最多保留 8 个),渲染按当前 sid 取自己的条目,旧版单槽位缓存读兼容并自动迁移;回归 +6

## [1.3.1] - 2026-08-15

### Fixed
- TPS 口径纠错并改实时:只计 output token(input/cacheRead 是每轮重发的上下文,v1.3.0 误计入后真机误显 5.9K/s,真实生成速度仅几十/s);速率=最近 3 次「llm.request→usage.record」配对的 output÷耗时均值(单次耗时含排队/思考/生成,即体感速度;业界 statusline 多为 tokens/min 燃烧率或会话平均,均非实时),空闲保留最后值不消失,老会话配对跌出尾部扫描窗口时回退会话平均;移除 60s 滚动窗口与 `TPS_WINDOW_S` 常量;显示单位 `t/s`

## [1.3.0] - 2026-08-13

### Added
- **实时 TPS**(tokens/s):最近 60s 滚动窗口聚合当前会话 wire.jsonl 的 `usage.record`(自尾向前扫描、跨出窗口即停,上限 2MB),显示在会话 token/金额段之后,空闲为 0 时隐藏;窗口大小可调(`TPS_WINDOW_S` 常量);回归 +4
- `tests/windows-e2e.ps1` Windows 真机验收脚本:用本机真实 Node 复刻 TUI 的 `spawn(cmd.exe, ['/d','/s','/c', command])` 链路验证引号解析、元字符路径(中文/空格/括号/&)压测、detached 刷新不闪窗(MainWindowHandle=0)、UTF-8 中文输出;CI windows job 接入(此前 TUI spawn 端到端链路零覆盖)

### Fixed
- Windows 真机三连修:install.py 的 `re.sub` 替换串吃掉路径双反斜杠写出非法 TOML(改 lambda 替换,`tomllib` 往返校验锁死);无空格/元字符路径改裸写 command(libuv quoting 把内嵌引号转 `\"` 喂给 cmd 导致命令每次失败、TUI 静默回退内置布局,裸写在全部已知 spawn 形态下可跑,含元字符退回引号形态并打警告);`tests/windows-e2e.ps1` 补 UTF-8 BOM(PS5.1 按 ANSI 解析中文炸语法)、探针改 verbatim+外包引号(与 kimi.exe 实跑一致)、修正 fake-home 的 `.kimi-code` 路径笔误
- Windows:引号回退兜底——元字符集补 `,` `;` `=`(cmd 参数分隔符,裸写路径会被切碎)
- Windows:`kimi doctor tui` 在 npm 安装的 `kimi.cmd` shim 下直接 CreateProcess 抛 WinError 193(OSError),`check=False` 拦不住,安装/卸载器会崩出 traceback——doctor 校验改经 `cmd /c`(shell=True)转一道并 `except OSError` 兜底只提示不阻断

### Changed
- README 首屏打磨:利益导向一句式 + 卖点速览列表 + 快速安装提前 + 去 stars 徽章(中英三处同步)

## [1.2.0] - 2026-08-13

### Added
- Windows 支持:后台刷新进程按平台分支(Windows 用 `DETACHED_PROCESS`,不再闪控制台窗口);stdin/stdout 强制 UTF-8(非 UTF-8 locale 下中文路径不再崩溃);跨平台安装器 `install.py` / `uninstall.py`(`install.sh` / `uninstall.sh` 保留为兼容壳)。注:TUI 真实拉起状态栏命令(带引号路径的解析、detached 不闪窗)在 Windows 真机上未做端到端验证,覆盖到 CI 冒烟为止
- 三平台 CI(`.github/workflows/ci.yml`):windows / ubuntu / macos 跑回归测试 + 中文路径冒烟渲染 + 安装/卸载往返
- README 顶部居中 hero 横幅(`assets/hero.svg`:品牌蓝渐变大标题 + 打字机轮换标语)+ 徽章行
- 安装完成后的一次性 Star 提示(安装器输出 + install 斜杠命令告知,只提一次)
- README 演示素材:状态栏静态截图 + swarm 水波动效 GIF(`assets/`,附生成器 make_demo.py)

## [1.1.2] - 2026-08-09

### Fixed
- 5h/7d 额度条弃用本地 token 折算回退(与官方窗口非线性,校准漂移导致 5h 误显 90%+):仅显示官方接口数据,超 10 分钟未更新压暗加 `~` 过期标记,无官方数据不显示
- swarm 标记在长会话中凭空消失:`swarm_mode.enter` 跌出 wire.jsonl 尾部 512K 扫描窗口;改为自文件尾向前分块扫描,取全文件最近一条 swarm_mode 记录(块内多条后者胜出)

### Added
- `tests/test_regressions.py` 回归测试(额度口径 4 例 + swarm 分块扫描 5 例)

## [1.1.1] - 2026-08-07

### Changed
- 消耗段由"今日"(全部会话当日聚合)改为"会话":token 与金额只统计当前会话的 wire.jsonl,不再跨会话总计
- 拉取官方额度接口的 User-Agent 版本号改为透传 CLI 快照的 `version` 字段,不再写死

## [1.1.0] - 2026-07-30

### Added
- swarm 模式动效：进入时品牌蓝(`#4FA8FF`,Kimi Code 官方主题 primary)水波自 `swarm` 标记向两侧扩散,约 8 秒后收敛为静态品牌蓝标记;双波干涉模拟水面质感
- 英文 README(README.md 切换为英文,中文移至 README.zh-CN.md)
- LICENSE(MIT)、CHANGELOG、issue/PR 模板、维护交接文档

### Changed
- 额度百分比优先使用官方 `GET /coding/v1/usages`(与 `/usage` 同源),10 分钟内有效;失败回退脚本内校准常量
- 金额按 K3 官方定价(输入 ¥20/M、缓存命中 ¥2/M、输出 ¥100/M)估算

## [1.0.0] - 2026-07-30

### Added
- 首个版本:权限模式 / 模型·思考强度 [上下文规格] / 5h·7d 额度条 / 今日 token 与金额 / git·目录
- 数据通道:TUI stdin JSON 快照 + 会话 wire.jsonl 重建 + 官方 usages 接口
- 安装/卸载脚本(install.sh / uninstall.sh,自动备份 + kimi doctor 校验)与插件斜杠命令
