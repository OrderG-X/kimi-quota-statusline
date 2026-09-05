# 维护交接文档(MAINTAINING)

> 本文档面向本项目的后续维护者(包括未来的自己/新会话的 Agent),说明架构、数据通道、开发调试与发布流程。
> 读完这份文档即可独立维护,不需要原始会话上下文。

## 一、这个项目是什么

Kimi Code CLI(≥0.30.0)的底部状态栏插件。本体只有一个文件:`statusline.py`(Python 3,零依赖)。
通过 `tui.toml` 的 `[status_line].command` 接入 TUI:TUI 每秒(硬编码上限)把 JSON 快照喂给 stdin,取 stdout 第一行渲染到底部第一行。

- 仓库:https://github.com/OrderG-X/kimi-quota-statusline
- 本机项目目录(维护真源):`/Users/guo/Projects/kimi-quota-statusline`
- 最终用户的安装形态:`/plugins install` 后由 CLI 拷贝到 `~/.kimi-code/plugins/managed/kimi-quota-statusline/`,`install.sh` 把 command 指向那里的 statusline.py

## 二、文件地图

| 文件 | 作用 |
|---|---|
| `statusline.py` | 状态栏本体(全部逻辑) |
| `kimi.plugin.json` | 插件清单(name/version/interface/commands);**发布时记得升 version** |
| `install.py` / `uninstall.py` | 跨平台幂等安装器:备份 tui.toml → 写入/移除 `[status_line].command` → `kimi doctor tui` 校验;`install.sh` / `uninstall.sh` 仅为 macOS/Linux 兼容壳(一行 exec 调 .py) |
| `commands/*.md` | 插件斜杠命令(`/kimi-quota-statusline:install|uninstall`),body 是给 Agent 的提示词 |
| `README.md` / `README.zh-CN.md` | 首页 README.md 为中文内联 + 英文 `<details>` 折叠;zh-CN 为独立中文文件;**任何行为变化必须三处同步(README.md 中英两段 + zh-CN)** |
| `CHANGELOG.md` | Keep a Changelog 格式 |
| `tests/test_regressions.py` | 回归测试(无框架):额度口径 / swarm 分块扫描 / TPS 窗口聚合 / 多会话缓存隔离 / 双 OAuth 凭据槽位与端点解析 / 后台任务段(计数/OSC 8 链接/看板/隐藏/动效剥除) / 看板服务(分组排序/HTTP 端点/段链接形态/幽灵降级/考古过滤/同源 /log/占位提示) / Windows 适配(detached 参数、stdio UTF-8、安装器行级匹配、doctor OSError 兜底、nt 命令形态)共 74 例,`python3 tests/test_regressions.py` |
| `tests/windows-e2e.ps1` | Windows 真机验收(PowerShell):真实 Node spawn 复刻 TUI 的 cmd /d /s /c 链路 + 元字符路径压测 + detached 不闪窗 + UTF-8;自动项已入 CI windows job,手动项(真实 TUI 肉眼)见脚本尾部清单 |
| `.github/workflows/ci.yml` | 三平台 CI(windows / ubuntu / macos):回归 + 中文路径冒烟渲染 + 安装/卸载往返 |
| `assets/` | `hero.svg`(README 顶部横幅:手写 SVG + SMIL 动画,品牌蓝渐变标题 + 三句打字机标语,改文案直接编辑;本地预览用 Chrome headless 截图)+ 演示素材 `statusline.png` / `swarm.gif` + 生成器 `make_demo.py`(依赖 Pillow,由 statusline.py 真实渲染逐帧生成;展示变化后重新跑一遍即可) |
| `docs/MAINTAINING.md` | 本文档 |

运行时产生的文件(在 `~/.kimi-code/`,不入库):
`statusline-tokens.json`(token/金额/官方额度缓存)、`statusline-stdin.json`(最近一次 stdin 快照,调试用)、`statusline-refresh.lock`(刷新锁)。

## 三、数据通道(改代码前必读)

1. **stdin 快照**(TUI → 脚本):`{model, cwd, gitBranch, permissionMode, planMode, contextUsage(0-1小数), contextTokens, maxContextTokens, sessionId, version}`。来源:`apps/kimi-code/src/tui/utils/status-line-command.ts`。
2. **会话 wire 日志**(`~/.kimi-code/sessions/*/<sessionId>/agents/main/wire.jsonl`,JSONL):
   - `config.update` / `llm.request` → 当前思考强度(`thinkingEffort`)
   - `swarm_mode.enter` / `swarm_mode.exit` → swarm 状态与进入时间(动效触发)
   - `usage.record` → token 消耗:`usage.{inputOther, output, inputCacheRead, inputCacheCreation}`,时间字段 `time`(epoch ms);TPS 由最近 60s 窗口(`TPS_WINDOW_S`)的 usage.record 聚合,自尾向前扫、跨出窗口即停(上限 2MB),空闲为 0 隐藏
3. **官方额度接口**:`GET {base_url}/usages`,Bearer 用凭据文件的 `access_token`(15 分钟有效期,CLI 运行时自动续)。**双 OAuth(CLI 0.38.0+)**:凭据槽位与 base_url 跟随 `~/.kimi-code/config.toml` 的 `[providers."managed:kimi-code"]`——`oauth.key` 剥掉 `oauth/` 前缀即 `credentials/<名>.json`(国际版 kimi.ai 是 `kimi-code-env-<hash>.json`,国内默认槽位仍是老的 `kimi-code.json`),`base_url` 决定端点域名;读不到配置(老版本 CLI)按国内默认。本脚本 `resolve_official_endpoint()` 按行解析该 TOML(Python 3.9 无 tomllib,只认 CLI 写出的固定形态)。返回 `usage`(周配额)+ `limits[]`(5h=300 TIME_UNIT_MINUTE),`used/limit` 为百分制。出处:kimi-code 仓库 `packages/oauth/src/managed-kimi-code.ts` + `managed-usage.ts`。
4. **额度显示口径**:仅用官方接口数据;超过 `OFFICIAL_FRESH_S`(600s)未更新压暗加 `~` 过期标记,从未拉到则不显示。本地 token 折算回退已于 v1.1.2 移除——与官方窗口非线性,校准漂移曾致 5h 误显 90%+(2026-08-09 用户报告),不要再加回来。
5. **后台任务**(v1.4.0 新增,v1.5.0 升级实时看板):`~/.kimi-code/sessions/*/<sessionId>/agents/main/tasks/<taskId>.json`,与 `/tasks` 面板同源;字段 `taskId/kind(process|agent|question)/status/description/startedAt/endedAt/pid`,process 输出在 `<taskId>/output.log`(静默任务可能尚未产生),agent 的转录在 `agents/<agentId>/wire.jsonl`(完成时 output.log 落最终结论)。有 running 才显示 `⚙ N` 段;段体包 OSC 8 超链接(`\033]8;;<url>\a…\033]8;;\a`),在 TUI 渲染链全程放行(零宽度、截断自动补关闭符、全屏模式点击走 openUrl),出处:`apps/kimi-code/src/tui/utils/status-line-command.ts` + `packages/pi-tui/src/utils.ts` + `tui-alt-screen.ts`。注意:swarm 动效通道必须剥 OSC 8(`OSC_RE`),否则当可见字符冲乱水波。看板 url 由 `board_url()` 决定:127.0.0.1 回环小服务(`--tasks-server` 入口,端口 18989-18998 自选,`statusline-tasks.port` 单实例;无 running 且无请求 15 分钟自灭,24h 绝对寿命)在线走 http,拉起中/失败回退静态 `statusline-tasks.html`(file://,内容不变零重写)。服务路由:`/` 页面(1s fetch 局部刷新)、`/data`(全会话总览 JSON:幽灵 running 降级——process 查 pid、agent/question 查 wire 120s 活跃度;2h 前考古任务过滤;重名项目补 sid 片段)、`/log?p=`(同源代读日志,白名单限 SESSIONS 内 .log/.jsonl 尾部 64KB,缺文件回占位提示)。

## 四、关键机制

- **增量缓存**:`refresh_cache()` 只扫当前会话 wire.jsonl(会话 token/金额)并拉官方额度;主流程发现缓存超过 `STALE_S`(20s)就 `Popen` 一个 detached `--refresh` 进程,自己用旧值先渲染 —— 状态栏永远 <50ms(预算 300ms)。缓存里会话条目按 sid 存**映射**(`sessions`,按最近活跃最多留 `MAX_CACHE_SESSIONS`=8 个)——单槽位时代多窗口会互相顶掉,token/金额/TPS 段周期性消失(v1.3.2 修复,别再退回单槽位)。
- **官方额度缓存**:`fetch_official()` 挂在 refresh 进程里,成功才覆盖,失败保留上次;超过 `OFFICIAL_FRESH_S`(600s)未更新则回退校准值。
- **swarm 动效**:`enter_ts` 来自最近一条 `swarm_mode.enter`,`elapsed < BURST_S`(8s)时整行走 `brand_flow()` 双波干涉水波;超时后只剩静态品牌蓝 `swarm` 段。重新进入会再次触发。
- **动画帧率上限**:TUI `STATUS_LINE_RERUN_INTERVAL_MS=1000` 硬编码,任何动效都是 1fps。已提 issue:[MoonshotAI/kimi-code#2396](https://github.com/MoonshotAI/kimi-code/issues/2396)(请求做成可配)。若未来官方放开,把 `brand_flow` 的速度参数调小即可变丝滑。

## 五、开发与调试

```bash
cd /Users/guo/Projects/kimi-quota-statusline
# 改 statusline.py 后,本机状态栏 1 秒内自动生效(tui.toml 指向本项目文件)

# 手动渲染测试(用最近一次真实快照):
cat ~/.kimi-code/statusline-stdin.json | python3 statusline.py
# 纯文本模式:
cat ~/.kimi-code/statusline-stdin.json | KIMI_SL_NOCOLOR=1 python3 statusline.py
# 强制重算 token 缓存:
python3 statusline.py --refresh
# 计时(必须远小于 300ms):
time (cat ~/.kimi-code/statusline-stdin.json | python3 statusline.py > /dev/null)
```

模拟 swarm 状态(不切换真实模式):在 `~/.kimi-code/sessions/wd_test_x/<sessionId>/agents/main/wire.jsonl` 写入伪造的 `swarm_mode.enter` 记录(time 用当前 epoch ms),stdin JSON 的 sessionId 指向它即可;测完删除 `wd_test_x`。

## 六、发布流程

1. 改代码 + 本地测试(上面清单 + `python3 tests/test_regressions.py` 回归测试);push 后确认三平台 CI 绿再发版。
2. 双语 README 同步;`CHANGELOG.md` 记录;`kimi.plugin.json` 的 `version` 升号。
3. `git add -A && git commit && git push`(origin = GitHub 仓库)。
4. 打 tag 并**发 GitHub Release**:`git tag v1.x.0 && git push --tags`,然后 `gh release create v1.x.0 --title v1.x.0 --notes <CHANGELOG 摘要>`。Release 的作用:repo URL 安装优先解析最新 Release(用户装到 pinned tag 而非浮动 HEAD);若未来进入官方市场目录,GitHub 源条目的目录版本也由最新 Release 解析。注意:**TUI 的更新提示只来自官方市场目录,与是否发 Release 无关**。
5. 已安装的用户侧升级:`/plugins` 面板 Installed 页会有更新提示,Enter 更新;或重新跑 install 命令。

## 七、CLI 更新后的兼容性巡检

Kimi Code 升级后(尤其跨 minor 版本),按本清单逐项核对;全部通过则无需改动,有失败项按「三、数据通道」定位修复。最近基线:CLI 0.41.0(2026-09-05 全部通过;跨 minor,0.40.1→0.41.0 changelog 无 status_line/stdin/usages/wire/oauth 相关条目;两个盯梢项实测均为虚惊——web 权限模式改名(#3549)只是文案,快照 `permissionMode` 实测仍小写枚举 `yolo`,三档着色不受影响;#3522 后台提问直投不动 tasks/*.json 结构,process/agent 样本依赖字段全在位,question 类暂无 0.41.0 实测样本,下次真机出现顺手抽查;当日真机快照 10 字段吻合,渲染与额度通道实测正常)。上一基线:CLI 0.40.1(2026-09-04 全部通过;跨 minor,changelog 无相关条目,仅 web 插件面板与 config.toml 写入优化;快照 10 字段与渲染当日真机确认)。

1. **官方 changelog 对照**:https://www.kimi.com/code/docs/en/kimi-code-cli/release-notes/changelog.html ,搜 status_line / plugin / wire / usages 相关条目。
2. **stdin 快照字段**:`cat ~/.kimi-code/statusline-stdin.json` —— 应含 `model, cwd, gitBranch, permissionMode, planMode, contextUsage, contextTokens, maxContextTokens, sessionId, version`。
3. **wire 记录存在性**:对当前会话 `~/.kimi-code/sessions/*/<sessionId>/agents/main/wire.jsonl` 分别 `grep -c` `usage.record` / `thinkingEffort` / `swarm_mode`,均应 >0。
4. **官方额度接口**:`python3 -c "import statusline; print(statusline.fetch_official())"` 应返回含 `wk_limit` 的 dict(token 过期时返回 None,属预期回退,先确认 CLI 在线再判失败)。
5. **配置校验**:`kimi doctor tui`。
6. **手动渲染 + 计时**:`cat ~/.kimi-code/statusline-stdin.json | python3 statusline.py` 单行无报错;`time` 实测远小于 300ms。

## 八、已知的坑(别再踩)

- 官方额度接口是 `/usages`(**复数**),不是 `/usage`。
- access_token 只有 900s 有效期,不要在脚本里用 refresh_token 自己续(会顶坏 CLI 的凭据轮换);过期就回退校准值,等 CLI 续上自然恢复。
- `[status_line].command` 只接管底部**第一行**;第二行(原生 context 读数)是 `footer.ts` 写死的,关不掉,所以本插件不显示 ctx 条(避免重复)。
- 不要把耗时操作放进主流程(300ms 超时会被 SIGKILL,整行回退内置布局)——重活一律走 detached refresh。
- 多行输出无效:只有 stdout 第一行会被渲染。
- Windows:后台刷新 Popen 必须用 `DETACHED_PROCESS`(否则闪控制台窗口),POSIX 才用 `start_new_session`;stdin 快照走 `sys.stdin.buffer` 按 UTF-8 解,stdout 也要 `reconfigure(encoding='utf-8')`(控制台文本层可能是 GBK/cp1252,print 中文直接 UnicodeEncodeError);tui.toml 里 Windows 路径反斜杠按 TOML 双写转义,卸载匹配前先归一化(分隔符跟随写入平台,别用 `os.path.join` 拼路径来比对)。
- 安装/卸载器对 tui.toml 的匹配一律**行级精确**:只认 command 行的值;注释或其他行提及插件路径不算数(section 级宽松匹配会误删指向他人脚本的 command,v1.2.0 评审发现并修复)。CI windows job 已用真实 Node spawn 复刻 TUI 的 cmd 引号解析 + detached 不闪窗(MainWindowHandle) + 元字符路径压测,但**真实 TUI 渲染**仍需真机肉眼确认。TUI 在 Windows 用 `cmd.exe /d /s /c` 解析 command,带引号路径含 `& ^ ( ) %` 等 cmd 元字符(如 Python 装在 `Program Files (x86)`)是最可能炸的点,`windows-e2e.ps1` 的 B 项专测这条。
- Windows:kimi 为 npm 安装时是 `kimi.cmd` shim,`subprocess.run(['kimi', ...])` 直接 CreateProcess 会抛 WinError 193(OSError);doctor 校验须 `shell=(os.name == 'nt')` 并经 `except OSError` 兜底只提示不阻断(v1.3.0 修复,回归锁死)。
- Windows 的 TUI spawn 引号语义(2026-08-13 真机实测,kimi.exe 用 verbatim 参数 + 外包引号):libuv 默认 quoting 会把内嵌引号转成 `\"` 喂给 cmd,cmd 不认反斜杠转义 → 带引号 command 每次失败、TUI 静默回退内置布局。因此 nt 命令**路径不含空格/cmd 元字符时裸写**(所有已知 spawn 形态都能跑),含元字符才退回引号形态(安装器此时打警告);元字符集含 `, ; =`(cmd 也当参数分隔符,`C:\a,b\x` 会被切碎)。
- `install.py` 覆盖已有 command 时的 `re.sub` 必须用 lambda 替换:替换串里的 Windows `\\` 会被当正则转义吃掉,写出非法 TOML(2026-08-13 真机事故,回归用 tomllib 往返校验锁死)。
- `tests/windows-e2e.ps1` 必须带 UTF-8 BOM 存盘:PS5.1 对无 BOM 文件按 ANSI 解析,中文注释直接语法错误。
- `/plugins` 面板(TUI)的更新提示**只由官方市场目录驱动**(2026-08-15 对照 MoonshotAI/kimi-code 源码逐行确认):Installed 页徽标 = 市场目录条目版本 vs 已装 manifest 版本,按 plugin id 匹配(`plugins-selector.ts installedUpdateStatus`,不查安装来源);主动通知仅官方插件(`plugin-update-notifier.ts`,GitHub 安装被 `isOfficialPluginInstall` 明确排除)。不在目录里的 GitHub 源插件**永远不会**有 TUI 更新提示——与 Release、会话缓存、R 刷新、重启均无关。`manager.checkUpdates()` 的 GitHub release/branch/SHA 比对只经 kap-server REST 服务 web UI,TUI 从不调用。目录在 `https://code.kimi.com/kimi-code/plugins/marketplace.json`(官方维护,GitHub 源 curated 条目的版本由 `withLatestVersions` 按最新 Release 运行时解析)。对照组:superpowers 在 Curated 目录所以提示正常;本插件不在目录,出路是申请进 Curated 市场,或用户手动重装升级。
- 缓存 schema 变更要**三处同步**:`statusline.py`、`tests/test_regressions.py`、`tests/windows-e2e.ps1` 的 C 段断言(v1.3.2 漏改 ps1 的 `$d.sess` 旧 schema 断言,windows CI 当场红;macOS/ubuntu 不跑该脚本所以没拦住)。另外 Edit 类工具改 ps1 会**吃掉 UTF-8 BOM**,改完必须 `head -c 3` 验证 `ef bb bf`,丢了要补回。
- 新会话快照时序:全新会话未发首条消息前,stdin 快照的 `maxContextTokens` 仍是默认值 262144(显示 256K),选了 1M 模型也要等对话真正开始才变 1048576——状态栏只是忠实渲染 CLI 给的值,首屏短暂显示 256K 属 CLI 侧行为,不是插件 bug(2026-08-24 真机确认,CLI 0.38.0)。
- 凭据文件写死老槽位:0.38.0 双 OAuth 起凭据按 (oauthHost, baseUrl) 的 sha256 前 16 位分槽位(`kimi-code-env-<hash>.json`),国际版登录后老的 `kimi-code.json` 不复存在,写死它会导致额度拉取静默失败、一直显示缓存里上一个账号的数据;槽位与端点必须从 config.toml 解析(v1.3.3 修复,回归锁死)。另:`api.kimi.ai` 裸请求(无 UA)会 403,带 `kimi-code-cli` UA 正常。
- **http 页面禁止跳 file:// 链接**(浏览器安全策略,Chrome/Safari 皆是):看板从 file:// 静态页升级为 http 服务后,页面里的 output/transcript 链接全部点不动(v1.5.0 真机发现)——日志必须由服务同源代读(`/log` 路由),别在 http 页面里放 file:// 链接。file:// 页面之间互跳不受此限。
- 看板服务的 Windows 真机验证待补:`--tasks-server` 的 detached 派生走的是与后台刷新相同的 `_detached_kwargs()`(DETACHED_PROCESS 形态),回归与 CI 覆盖不了真实 TUI 环境,Windows 下首次拉起看板服务需真机肉眼确认一次(macOS 已验证);失败时回退静态 file:// 板,功能降级但不挂。
- 发版顺序纪律:tag/release 必须在 push 后**等三平台 CI 全绿**再打(v1.5.0 把 commit/push/tag/release 串成一批命令,windows CI 红被烙进 tag,只能发 v1.5.1 补救)。另外 POSIX 专属 API(如 `os.kill(pid, 0)`)加 `os.name` 守卫跳过会让行为平台分叉、Windows 测试当场露馅——跨平台行为要统一实现(如 `_pid_alive` 的 Windows OpenProcess 路径),别用守卫回避。

## 九、路线图(想法池)

- Extra Usage 钱包余额段(接口 `boosterWallet` 已返回,目前 STATUS_DISABLED 未启用)
- 并发会话段(接口 `parallel`:limit 30 + 活跃会话数)
- 官方若放开刷新间隔(issue #2396):动效改 10fps
- per-model 定价表(目前统一按 K3)
