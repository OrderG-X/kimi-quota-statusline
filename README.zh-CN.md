<div align="center">

<img src="assets/hero.svg" alt="Kimi Quota Statusline — Kimi Code CLI 状态栏增强" width="760" />

<p>
  <a href="https://github.com/OrderG-X/kimi-quota-statusline/tags"><img src="https://img.shields.io/github/v/tag/OrderG-X/kimi-quota-statusline?label=version&color=4fa8ff" alt="version" /></a>
  <img src="https://img.shields.io/badge/license-MIT-blue" alt="License: MIT" />
  <img src="https://img.shields.io/badge/python-3-3776ab?logo=python&logoColor=white" alt="Python 3" />
  <img src="https://img.shields.io/badge/kimi--code-%E2%89%A5%200.30.0-4fa8ff" alt="Kimi Code >= 0.30.0" />
</p>

<p>
  <b>中文文档</b> ·
  <a href="README.md#english">English</a>
</p>

</div>

---

Kimi Code CLI 状态栏增强插件（statusline plugin)：**额度、消耗、swarm 状态一眼看清**。额度数据与 `/usage` 完全同源（直连官方 `GET /coding/v1/usages` 接口）。

![状态栏效果](assets/statusline.png)

进入 **swarm 模式**时，品牌蓝(`#4FA8FF`）水波自 `swarm` 标记处向两侧荡开，约 8 秒后收敛为静态品牌蓝标记：

![swarm 水波动效](assets/swarm.gif)

- ✅ `5h` / `7d` 官方额度条：6 格进度 + 已用百分比 + 重置倒计时，绿/黄/红三档
- ✅ 本会话 token、估算金额与**实时 TPS**（最近数次请求的生成速度均值，空闲保留最后值）
- ✅ 权限模式 / 模型·思考强度 / git 分支 / 项目目录
- ✅ swarm 模式品牌蓝水波动效（约 8 秒）
- ✅ **后台任务段**：有 running 的后台任务/子 agent 时显示 `⚙ N`，点击打开本地实时任务看板（全会话总览，1s 局部刷新，output/transcript 同源链接可看子代理在干嘛）
- ✅ **额度提醒 hook**（三端生效）：每条消息把实时额度静默注入上下文（模型随时答得出"还剩多少"）,5h 过半 / 7d 超八成时模型自动在回复末尾亮出 ⏱ 提醒
- ⚡ 单次运行 < 50ms（token 统计走缓存,TPS 走尾部限量扫描,300ms 预算内）

## 快速安装

方式一（推荐，作为插件），在 Kimi Code 里依次运行两条命令。

第一步，安装插件：

```
/plugins install https://github.com/OrderG-X/kimi-quota-statusline
```

第二步，接管状态栏：

```
/kimi-quota-statusline:install
```

方式二（手动）:

```bash
git clone https://github.com/OrderG-X/kimi-quota-statusline.git
python3 kimi-quota-statusline/install.py   # Windows: python kimi-quota-statusline\install.py
# 自动备份 tui.toml、写入 [status_line].command、kimi doctor 校验；然后在 TUI 运行 /reload-tui
```

要求：Kimi Code CLI ≥ 0.30.0（`[status_line]` 特性）、Python 3、macOS / Linux / Windows。

## 显示内容

| 段 | 内容 | 数据来源 |
|---|---|---|
| 权限模式 | `YOLO` / `AUTO` / `MANUAL`（大写，分色） | 状态栏 stdin 快照 |
| 模型·思考强度 `[上下文]` | `K3·max [1M]`，强度从当前会话 wire 日志重建 | wire.jsonl |
| swarm 特效 | 进入时品牌蓝水波双向扩散（~8s），随后静态蓝标 | wire.jsonl `swarm_mode.*` |
| `5h` / `7d` 额度条 | 6 格进度条 + 已用百分比 + 重置倒计时，绿/黄/红三档 | **官方 `/usages` 接口**（超 10 分钟未更新压暗加 `~`，无数据不显示） |
| 本会话消耗 | token 量 + 估算金额 + 实时 TPS（仅当前会话，不含其他会话） | 当前会话 wire.jsonl `usage.record` 聚合 |
| git 分支 / 项目目录 | ⎇ 分支、目录名 | stdin 快照 |

## 金额口径

按 Kimi 开放平台官方定价逐条累计（K3：输入 ¥20/百万 token、缓存命中 ¥2/百万、输出 ¥100/百万；缓存创建按标准输入价计）。是"等值估算"，套餐内实际不扣费。

## 卸载

```
/kimi-quota-statusline:uninstall   # 或手动:python3 uninstall.py(Windows: python uninstall.py)
```

恢复官方默认状态栏。

## 工作原理

- `tui.toml` 的 `[status_line].command` 指向 `statusline.py`,TUI 每秒以内把 JSON 快照（模型/目录/git/权限/上下文用量/sessionId）喂给它，取 stdout 第一行渲染
- 思考强度与 swarm 状态：快照没有，从当前会话 `~/.kimi-code/sessions/*/<sessionId>/agents/main/wire.jsonl` 尾部记录重建
- token/金额：5h/7d 聚合全部会话 wire.jsonl 的 `usage.record`;"会话"段只聚合当前会话的 wire.jsonl（按文件 mtime 增量缓存；重活由 detached 后台进程刷新，状态栏单次运行 <50ms，远低于 300ms 预算）
- 额度百分比：官方 `GET https://api.kimi.com/coding/v1/usages`(Bearer 用本地 OAuth token，与 `/usage` 命令同源，见 kimi-code 仓库 `packages/oauth/src/managed-usage.ts`)；超过 10 分钟未更新压暗加 `~` 过期标记，从未拉到则不显示（本地 token 折算已于 v1.1.2 移除：与官方窗口非线性，校准漂移曾致误显）
- 动画帧率受 TUI 硬编码的 1 秒重跑间隔限制（`STATUS_LINE_RERUN_INTERVAL_MS`)，可配置化请求见 [issue #2396](https://github.com/MoonshotAI/kimi-code/issues/2396)

## 配置

- `KIMI_SL_NOCOLOR=1`：关闭 ANSI 颜色（纯文本）
- `statusline.py` 顶部常量：`PRICE_*`（定价）、`USAGES_URL`、`OFFICIAL_FRESH_S`（官方数据新鲜度阈值）、`BURST_S`（特效时长）

## License

[MIT](LICENSE)
