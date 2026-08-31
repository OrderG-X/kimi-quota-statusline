#!/usr/bin/env python3
"""kimi-code 底部状态栏脚本(tui.toml [status_line].command)

stdin 收到 CLI 的 JSON 快照,stdout 替换底部状态栏(支持两行)。
运行预算 300ms,每秒最多一次 —— token 统计走缓存,重活由后台 detached 进程刷新。

第一行:权限 · 模型·强度 [上下文规格] · swarm · ⚙ 后台任务(running 才显示,OSC 8 可点击) · 5h/7d 额度条(官方接口) · 会话 token/金额/TPS · git · 目录
- 5h/7d 额度:仅官方 /usages 接口;过期压暗加 ~ 标记,从未拉到则不显示
  (本地 token 折算与官方窗口非线性、校准持续漂移,已于 v1.1.2 移除)
- 本会话 token/金额:仅当前会话 wire.jsonl 的 usage.record 聚合
- TPS:实时生成速度——最近 3 次「llm.request→usage.record」配对的 output÷耗时均值,空闲保留最后值;老会话配对跌出尾部窗口时回退会话平均
- 思考强度/swarm:当前会话 wire.jsonl 自尾向前分块重建(swarm 取全文件最近一条记录)
- ANSI 彩色;KIMI_SL_NOCOLOR=1 回退纯文本
"""
import glob
import json
import os
import subprocess
import sys
import time

HOME = os.path.expanduser('~/.kimi-code')
SESSIONS = os.path.join(HOME, 'sessions')
CACHE = os.path.join(HOME, 'statusline-tokens.json')
LOCK = os.path.join(HOME, 'statusline-refresh.lock')
DEBUG_STDIN = os.path.join(HOME, 'statusline-stdin.json')
STALE_S = 20        # 缓存超过 20s 触发后台刷新
LOCK_S = 60         # 刷新锁,避免并发刷新
TAIL_BYTES = 524288
MAX_CACHE_SESSIONS = 8  # 缓存里会话条目的上限,按最近活跃裁剪,防文件无限长大

# 官方额度接口(源码 packages/oauth/src/managed-usage.ts):GET {base}/usages,Bearer 认证
# 返回 usage(周配额)+ limits[](5h 等窗口)+ boosterWallet;used/limit 为百分制字符串
USAGES_URL = 'https://api.kimi.com/coding/v1/usages'
CRED_DIR = os.path.join(HOME, 'credentials')
CRED_FILE = os.path.join(CRED_DIR, 'kimi-code.json')  # 国内版默认槽位(无 hash 老文件名)
CONFIG_FILE = os.path.join(HOME, 'config.toml')
OFFICIAL_FRESH_S = 600  # 官方数据 10 分钟内为新鲜;过期压暗加 ~ 标记,不再回退本地折算


def resolve_official_endpoint():
    """双 OAuth(CLI 0.38.0+):凭据槽位与 usages 端点跟随 config.toml 的当前登录环境。

    登录国际版(kimi.ai)后凭据在 kimi-code-env-<hash>.json、端点是 api.kimi.ai,
    都写在 config.toml 的 [providers."managed:kimi-code"] 里;读不到配置(老版本
    CLI)回退国内默认槽位。Python 3.9 无 tomllib,这里只按行解析 CLI 写出的固定形态。
    """
    name, url = None, None
    try:
        section = ''
        with open(CONFIG_FILE, encoding='utf-8', errors='replace') as f:
            for raw in f:
                line = raw.strip()
                if line.startswith('['):
                    section = line.strip('[] ')
                elif '=' in line and not line.startswith('#'):
                    k, _, v = line.partition('=')
                    v = v.strip().strip('"')
                    if section == 'providers."managed:kimi-code"' and k.strip() == 'base_url' and v:
                        url = v.rstrip('/') + '/usages'
                    elif section == 'providers."managed:kimi-code".oauth' and k.strip() == 'key' and v:
                        name = v.split('/')[-1]  # 剥掉 oauth/ 前缀,即凭据文件名
    except OSError:
        pass
    cred = os.path.join(CRED_DIR, name + '.json') if name else CRED_FILE
    return cred, (url or USAGES_URL)

# Kimi K3 官方定价(元/百万 token,2026-07 开放平台公示):
# 输入(未命中缓存)20、输入(缓存命中)2、输出 100;缓存创建按标准输入价计
PRICE_INPUT = 20.0
PRICE_OUTPUT = 100.0
PRICE_CACHE_READ = 2.0

USE_ANSI = os.environ.get('KIMI_SL_NOCOLOR') != '1'
RESET, BOLD, DIM = '\033[0m', '\033[1m', '\033[2m'
REVERSE = '\033[7m'

# swarm 动效:进入 swarm 的前几秒品牌蓝扫描带,随后收敛为静态标记
BRAND = (0x4F, 0xA8, 0xFF)      # Kimi Code 官方主题 primary #4FA8FF
BRAND_DIM = (0x24, 0x4E, 0x80)
BURST_S = 8.0                   # 扫描特效持续秒数
ANSI_RE = __import__('re').compile(r'\033\[[0-9;]*m')
OSC_RE = __import__('re').compile(r'\033\][^\a]*\a')  # OSC 8 超链接等(BEL 结尾),动效前要剥掉


def brand_fg(text, rgb, *extra):
    if not USE_ANSI:
        return text
    return f'\033[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m' + ''.join(extra) + str(text) + RESET


def brand_flow(text, elapsed):
    """品牌蓝水波:以 swarm 为波心向两侧扩散,双波干涉出水花。
    受限于 TUI 状态栏 1 次/秒的运行上限(status-line-command.ts:
    STATUS_LINE_RERUN_INTERVAL_MS=1000,Claude Code 同款契约),
    已调到该帧率下的最大流畅度:主波每秒 ~9 字符 + 反向次波干涉。"""
    import math
    center = text.find('swarm')
    center = center + 2 if center >= 0 else len(text) // 2
    out = []
    for i, ch in enumerate(text):
        if ch == ' ':
            out.append(' ')
            continue
        d = abs(i - center)
        w1 = math.sin(d * 0.30 - elapsed * 2.6)        # 主波:双向快推
        w2 = math.sin(d * 0.13 + elapsed * 1.9)        # 次波:反向慢回,干涉
        v = (w1 + 0.6 * w2) / 1.6
        v = (v + 1) / 2
        v = 0.25 + 0.75 * (v ** 1.3)                    # 暗部保底亮度,全程可读
        r = int(BRAND_DIM[0] + (225 - BRAND_DIM[0]) * v)
        g = int(BRAND_DIM[1] + (238 - BRAND_DIM[1]) * v)
        b = int(BRAND_DIM[2] + (255 - BRAND_DIM[2]) * v)
        out.append(f'\033[38;2;{r};{g};{b}m{ch}')
    out.append(RESET)
    return ''.join(out)
CYAN, GREEN, YELLOW, RED, MAGENTA, BLUE, GRAY = (
    '\033[36m', '\033[32m', '\033[33m', '\033[31m', '\033[35m', '\033[34m', '\033[90m')


def c(text, *codes):
    if not USE_ANSI or not text:
        return text
    return ''.join(codes) + str(text) + RESET


def sep():
    return c(' · ', DIM)


def fmt_tokens(n):
    if n >= 1_000_000:
        return f'{n / 1_000_000:.1f}M'
    if n >= 1_000:
        return f'{n / 1_000:.1f}K'
    return str(n)


def fmt_ctx(n):
    """上下文规格:1024 进制,1048576→1M,262144→256K。"""
    if n >= 1048576:
        return f'{n / 1048576:g}M'
    return f'{round(n / 1024)}K'


# ---------- token 聚合 + 官方额度(后台刷新进程执行) ----------
def fetch_official(ver=''):
    """拉官方额度:周配额(usage)+ 5h 窗口(limits[])。token 过期或失败返回 None。"""
    import urllib.request
    try:
        cred_file, usages_url = resolve_official_endpoint()
        cred = json.load(open(cred_file))
        if cred.get('expires_at', 0) < time.time() + 10:
            return None
        req = urllib.request.Request(usages_url, headers={
            'Authorization': f"Bearer {cred['access_token']}",
            'Accept': 'application/json',
            'User-Agent': f'kimi-code-cli/{ver}' if ver else 'kimi-code-cli'})
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode())
        out = {'ts': time.time()}
        wk = d.get('usage') or {}
        if wk.get('limit'):
            out['wk_used'] = float(wk.get('used', 0))
            out['wk_limit'] = float(wk['limit'])
            out['wk_reset'] = wk.get('resetTime', '')
        for item in d.get('limits') or []:
            w = item.get('window') or {}
            if w.get('duration') == 300 and w.get('timeUnit') == 'TIME_UNIT_MINUTE':
                det = item.get('detail') or {}
                if det.get('limit'):
                    out['h5_used'] = float(det.get('used', 0))
                    out['h5_limit'] = float(det['limit'])
                    out['h5_reset'] = det.get('resetTime', '')
        return out if len(out) > 1 else None
    except Exception:
        return None


def refresh_cache(session_id='', ver=''):
    # 本会话 token/金额:只扫当前会话的 wire.jsonl
    # (5h/7d 不再本地折算:与官方窗口非线性,校准漂移导致误显,已于 v1.1.2 移除聚合)
    sess = None
    if session_id:
        hits = glob.glob(os.path.join(SESSIONS, '*', session_id, 'agents', 'main', 'wire.jsonl'))
        if hits:
            n, amt, out = 0, 0.0, 0
            t0 = t1 = None
            try:
                with open(hits[0], 'rb') as f:
                    for line in f:
                        if b'usage.record' not in line:
                            continue
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        if rec.get('type') != 'usage.record':
                            continue
                        u = rec.get('usage', {})
                        n += (u.get('inputOther', 0) + u.get('output', 0)
                              + u.get('inputCacheRead', 0) + u.get('inputCacheCreation', 0))
                        amt += (u.get('inputOther', 0) * PRICE_INPUT
                                + u.get('output', 0) * PRICE_OUTPUT
                                + u.get('inputCacheRead', 0) * PRICE_CACHE_READ
                                + u.get('inputCacheCreation', 0) * PRICE_INPUT) / 1e6
                        # 会话平均 TPS 的原料:累计 output + 首/末记录时间(记录按时间序)
                        out += u.get('output', 0)
                        t = rec.get('time', 0)
                        if t:
                            if t0 is None:
                                t0 = t
                            t1 = t
            except OSError:
                pass
            sess = {'id': session_id, 'tokens': n, 'cost': amt,
                    'out': out, 't0': t0, 't1': t1}
    tmp = CACHE + '.tmp'
    # 官方额度:拉取成功则更新,失败保留上次结果
    official = fetch_official(ver)
    try:
        prev = json.load(open(CACHE))
    except Exception:
        prev = {}
    if official is None:
        official = prev.get('official')
    # 会话条目用映射存储:并发会话各自刷新只写自己的槽位;旧版单槽位
    # ('sess')会被另一窗口的刷新顶掉,导致 token/金额/TPS 段周期性消失
    sessions = dict(prev.get('sessions') or {})
    legacy = prev.get('sess')
    if isinstance(legacy, dict) and legacy.get('id'):
        sessions.setdefault(legacy['id'], legacy)
    if sess is not None:
        sessions[session_id] = sess
    if len(sessions) > MAX_CACHE_SESSIONS:
        sessions = dict(sorted(sessions.items(),
                               key=lambda kv: kv[1].get('t1') or 0)[-MAX_CACHE_SESSIONS:])
    try:
        with open(tmp, 'w') as f:
            json.dump({'ts': time.time(), 'sessions': sessions, 'official': official}, f)
        os.replace(tmp, CACHE)
    except OSError:
        pass
    try:
        os.remove(LOCK)
    except OSError:
        pass


def _detached_kwargs(os_name=os.name):
    """后台刷新进程的 Popen 平台参数:POSIX 脱离会话;Windows 用 DETACHED_PROCESS,
    否则每次刷新都会闪一个控制台窗口(常量值即 Win32 API 值,POSIX 的 subprocess
    没有这两个属性,故用字面量)。"""
    if os_name == 'nt':
        # DETACHED_PROCESS(0x8) | CREATE_NEW_PROCESS_GROUP(0x200)
        return {'creationflags': 0x00000008 | 0x00000200}
    return {'start_new_session': True}


def load_tokens(session_id='', ver=''):
    """读缓存;过期则 detached 刷新(带上 sessionId 统计本会话消耗、ver 作 UA),当前用旧值先显示。"""
    try:
        cache = json.load(open(CACHE))
    except Exception:
        cache = None
    stale = not cache or (time.time() - cache.get('ts', 0)) > STALE_S
    if stale:
        try:
            if not os.path.exists(LOCK) or time.time() - os.stat(LOCK).st_mtime > LOCK_S:
                open(LOCK, 'w').close()
                subprocess.Popen([sys.executable, os.path.abspath(__file__), '--refresh', session_id, ver],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, **_detached_kwargs())
        except OSError:
            pass
    return cache or {}


def pick_sess(cache, sid):
    """从缓存取本会话条目:sessions 映射按 sid 取;旧版单槽位按 id 兜底(读兼容)。"""
    if not sid or not cache:
        return {}
    s = (cache.get('sessions') or {}).get(sid)
    if s:
        return s
    legacy = cache.get('sess') or {}
    return legacy if legacy.get('id') == sid else {}


def reset_hint(iso):
    """ISO 时间 → 紧凑倒计时:13h / 2h14m / 48m。"""
    if not iso:
        return ''
    try:
        from datetime import datetime
        ts = datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp()
        s = int(ts - time.time())
        if s <= 0:
            return ''
        d, s = divmod(s, 86400)
        h, s = divmod(s, 3600)
        m = s // 60
        if d:
            return f'{d}d{h}h'
        if h:
            return f'{h}h{m}m' if m else f'{h}h'
        return f'{m}m'
    except Exception:
        return ''


def session_state(session_id):
    """从当前会话 wire.jsonl 重建 (思考强度, swarm是否激活, 最近一次进入时间)。
    自文件尾向前分块扫描:两者都取全文件最近一条相关记录。长会话的
    swarm_mode.enter 会跌出单个尾部窗口,不向前翻块会导致 swarm 标记凭空消失。"""
    effort, swarm, enter_ts = '', False, 0.0
    if not session_id:
        return effort, swarm, enter_ts
    hits = glob.glob(os.path.join(SESSIONS, '*', session_id, 'agents', 'main', 'wire.jsonl'))
    if not hits:
        return effort, swarm, enter_ts
    try:
        size = os.path.getsize(hits[0])
        with open(hits[0], 'rb') as f:
            end = size
            got_effort = got_swarm = False
            while end > 0 and not (got_effort and got_swarm):
                start = max(0, end - TAIL_BYTES)
                f.seek(start)
                lines = f.read(end - start).splitlines()
                if start > 0 and lines:
                    lines = lines[1:]  # 块首可能是半行,丢弃
                # need_* 在进块时快照:块内多条记录后者胜出(时间序),已命中类型的旧块整段跳过
                need_effort = not got_effort
                need_swarm = not got_swarm
                for line in lines:
                    if b'thinkingEffort' in line and need_effort:
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        e = r.get('thinkingEffort')
                        if not e and isinstance(r.get('event'), dict):
                            e = r['event'].get('thinkingEffort')
                        if e:
                            effort = e
                            got_effort = True
                    elif b'swarm_mode' in line and need_swarm:
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        t = r.get('type', '')
                        if t == 'swarm_mode.enter':
                            swarm = True
                            enter_ts = r.get('time', 0) / 1000
                            got_swarm = True
                        elif t == 'swarm_mode.exit':
                            swarm = False
                            got_swarm = True
                end = start
    except OSError:
        return effort, swarm, enter_ts
    return effort, swarm, enter_ts


def session_tps(sess):
    """会话平均生成速度(output tokens/s):累计 output ÷ 活跃时长(首条→末条记录)。
    只计 output:input/cacheRead 是每轮重发的上下文,不是吞吐(v1.3.0 曾误计入
    并除以固定窗口,真机误显 5.9K/s,真实生成速度仅几十/s)。不足 2 条记录返回 0。
    现作 live_tps 的兜底(老会话最近一次配对跌出尾部扫描窗口时用)。"""
    out, t0, t1 = sess.get('out', 0), sess.get('t0'), sess.get('t1')
    if not out or not t0 or not t1 or t1 <= t0:
        return 0.0
    return out / ((t1 - t0) / 1000)


def live_tps(session_id, max_blocks=4, pair_n=3):
    """实时生成速度(output tokens/s):最近 pair_n 次「llm.request → usage.record」
    配对的均值。单次耗时含排队/思考/生成,即用户体感速度——这是 wire.jsonl 无
    逐条生成耗时字段下最诚实的实时口径(业界 statusline 多用 tokens/min 燃烧率
    或会话平均,都不是实时)。自尾部向前分块扫,凑够 pair_n 对即停;
    配对 sanity:耗时 ≤600s;无配对返回 0。
    已知取舍:块边界记录可能双侧各丢半条(新块丢首部半行、旧块尾部半行解析
    失败),每次扫描最多丢几条,对 TPS 估值影响可忽略。"""
    if not session_id:
        return 0.0
    hits = glob.glob(os.path.join(SESSIONS, '*', session_id, 'agents', 'main', 'wire.jsonl'))
    if not hits:
        return 0.0
    reqs, recs = [], []
    try:
        size = os.path.getsize(hits[0])
        with open(hits[0], 'rb') as f:
            end = size
            blocks = 0
            while end > 0 and blocks < max_blocks:
                start = max(0, end - TAIL_BYTES)
                f.seek(start)
                lines = f.read(end - start).splitlines()
                if start > 0 and lines:
                    lines = lines[1:]  # 块首可能是半行,丢弃
                blocks += 1
                for line in lines:
                    if b'llm.request' in line:
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        # type 校验不可省:payload 文本可能含 "llm.request" 子串,
                        # 幻影请求点会压短耗时、抬高 TPS(正是要消灭的误显方向)
                        if r.get('type') != 'llm.request':
                            continue
                        t = r.get('time', 0)
                        if t:
                            reqs.append(t)
                    elif b'usage.record' in line:
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        if r.get('type') == 'usage.record' and r.get('time'):
                            recs.append((r['time'], r.get('usage', {}).get('output', 0)))
                if (len(recs) >= pair_n and len(reqs) >= pair_n) or start == 0:
                    break
                end = start
    except OSError:
        return 0.0
    import bisect
    reqs.sort()
    speeds = []
    for t, out in sorted(recs, reverse=True):  # 新的优先
        i = bisect.bisect_right(reqs, t) - 1
        if i < 0:
            continue
        dur = (t - reqs[i]) / 1000
        if 0 < dur <= 600 and out > 0:
            speeds.append(out / dur)
        if len(speeds) >= pair_n:
            break
    return sum(speeds) / len(speeds) if speeds else 0.0


# ---------- 后台任务段(tasks/*.json → ⚙ N + OSC 8 看板链接) ----------
TASKS_HTML = os.path.join(HOME, 'statusline-tasks.html')


def session_tasks(sid):
    """当前会话的后台任务记录(bash/agent/question 都在 main agent 的 tasks/ 下,与 /tasks 面板同源)。"""
    out = []
    if not sid:
        return out
    for p in glob.glob(os.path.join(SESSIONS, '*', sid, 'agents', 'main', 'tasks', '*.json')):
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                t = json.load(f)
        except Exception:
            continue
        if isinstance(t, dict) and t.get('taskId'):
            t['_log'] = os.path.join(os.path.dirname(p), str(t['taskId']), 'output.log')
            out.append(t)
    return out


def _fmt_dur(ms):
    s = max(0, int(ms / 1000))
    return f'{s // 60}m{s % 60:02d}s' if s >= 60 else f'{s}s'


def render_tasks_html(tasks):
    """后台任务看板(点状态栏 ⚙ 段在浏览器打开);内容不变不重写,避免每秒 IO。"""
    import html as _h
    now_ms = time.time() * 1000
    st_color = {'running': '#4fa8ff', 'completed': '#3fb950'}
    rows = []
    for t in sorted(tasks, key=lambda x: x.get('startedAt', 0), reverse=True)[:30]:
        st = str(t.get('status', ''))
        color = st_color.get(st, '#f85149' if st in ('failed', 'timed_out', 'killed') else '#8b949e')
        desc = _h.escape(str(t.get('description') or t.get('command') or t['taskId']))[:120]
        dur = _fmt_dur((t.get('endedAt') or now_ms) - t.get('startedAt', 0))
        log = _h.escape('file://' + t.get('_log', ''), quote=True)
        rows.append(f'<tr><td style="color:{color}">{_h.escape(st)}</td>'
                    f'<td>{_h.escape(str(t.get("kind", "")))}</td><td>{desc}</td>'
                    f'<td>{dur}</td><td><a href="{log}">output</a></td></tr>')
    n_run = sum(1 for t in tasks if t.get('status') == 'running')
    doc = ('<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="2">'
           '<title>后台任务 · kimi-quota-statusline</title>'
           '<style>body{background:#0d1117;color:#c9d1d9;font:14px/1.6 -apple-system,monospace;'
           'padding:20px;max-width:900px;margin:auto}table{border-collapse:collapse;width:100%}'
           'td{padding:4px 10px;border-bottom:1px solid #21262d}a{color:#4fa8ff}</style>'
           f'<h3>⚙ 后台任务({n_run} running / {len(tasks)} total)</h3><table>' + ''.join(rows) + '</table>')
    try:
        with open(TASKS_HTML, encoding='utf-8') as f:
            if f.read() == doc:
                return
    except OSError:
        pass
    try:
        with open(TASKS_HTML, 'w', encoding='utf-8') as f:
            f.write(doc)
    except OSError:
        pass


def task_segment(tasks):
    """后台任务段:有 running 才显示;整段 OSC 8 超链接到任务看板(实时 http 服务优先,回退静态页)。"""
    n = sum(1 for t in tasks if t.get('status') == 'running')
    if not n:
        return None
    url = board_url()
    if not url:
        render_tasks_html(tasks)
        url = 'file://' + TASKS_HTML
    label = c(f'⚙ {n}', YELLOW, BOLD)
    return f'\033]8;;{url}\a{label}\033]8;;\a'


# ---------- 实时任务看板服务(仅 127.0.0.1 回环;/data 每秒局部刷新,全会话总览) ----------
TASKS_PORT_FILE = os.path.join(HOME, 'statusline-tasks.port')
TASKS_PORTS = range(18989, 18999)
SERVER_IDLE_S = 900       # 无 running 且无请求 15 分钟自灭
SERVER_MAX_AGE_S = 86400  # 绝对寿命 24h,防跨版本僵尸


def _pid_alive(pid):
    """pid 存活检查:POSIX 用 kill(0);Windows 用 OpenProcess + GetExitCodeProcess
    (os.kill(pid, 0) 在 Windows 不支持,返回 STILL_ACTIVE(259) 才算活)。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == 'nt':
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong(0)
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def tasks_payload():
    """全会话后台任务总览:按会话分组,会话内 running 在前、新的在前;有 running 的会话排前。
    展示层修正(不改任务记录,属主会话自己管):
    - 幽灵 running 降级 lost:process 任务查 pid 存活(POSIX;Windows 跳过按原样),
      agent/question 任务看所属会话 wire.jsonl 活跃度(120s 无写入即死会话)
    - 已完成任务只保留近 2h,更早的是考古数据不进看板
    """
    now_ms = time.time() * 1000
    groups = {}
    for p in glob.glob(os.path.join(SESSIONS, '*', '*', 'agents', 'main', 'tasks', '*.json')):
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                t = json.load(f)
        except Exception:
            continue
        if not (isinstance(t, dict) and t.get('taskId')):
            continue
        if t.get('status') == 'running':
            if t.get('kind') == 'process' and t.get('pid'):
                if not _pid_alive(t['pid']):
                    t['status'] = 'lost'
            elif t.get('kind') != 'process':
                wire = os.path.join(os.path.dirname(os.path.dirname(p)), 'wire.jsonl')
                try:
                    if time.time() - os.stat(wire).st_mtime > 120:
                        t['status'] = 'lost'
                except OSError:
                    t['status'] = 'lost'
        elif now_ms - (t.get('endedAt') or t.get('startedAt') or 0) > 2 * 3600 * 1000:
            continue  # 2h 前的已结束任务:考古数据
        parts = p.split(os.sep)  # …/sessions/<wd>/<sid>/agents/main/tasks/<tid>.json
        sid = parts[-5] if len(parts) >= 5 else ''
        wd = parts[-6] if len(parts) >= 6 else ''
        label = wd[3:] if wd.startswith('wd_') else wd
        label = label.rsplit('_', 1)[0] or label  # 去目录名末尾的 hash 后缀
        t['_log'] = os.path.join(os.path.dirname(p), str(t['taskId']), 'output.log')
        # 看板链接走同源 /log 路由(http 页面禁止跳 file://):agent 链 wire 转录,其余链 output.log
        from urllib.parse import quote
        if t.get('kind') == 'agent' and t.get('agentId'):
            wire = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(p))),
                                str(t['agentId']), 'wire.jsonl')
            if os.path.isfile(wire):
                t['_log_url'] = '/log?p=' + quote(wire)
        if '_log_url' not in t:  # 静默任务 output.log 可能还没产生,链接照给(/log 会回占位提示)
            t['_log_url'] = '/log?p=' + quote(t['_log'])
        groups.setdefault((sid, label), []).append(t)
    sessions = []
    for (sid, label), tasks in groups.items():
        tasks.sort(key=lambda x: (x.get('status') != 'running', -(x.get('startedAt') or 0)))
        sessions.append({'sid': sid, 'label': label, 'tasks': tasks})
    sessions.sort(key=lambda s: (not any(t.get('status') == 'running' for t in s['tasks']),
                                 -max((t.get('startedAt') or 0) for t in s['tasks'])))
    dup = {}
    for s in sessions:
        dup[s['label']] = dup.get(s['label'], 0) + 1
    for s in sessions:  # 同项目多会话:标签补 sid 片段消歧
        if dup[s['label']] > 1:
            s['label'] = f"{s['label']} · {s['sid'].replace('session_', '')[:8]}"
    return {'generated': time.time(), 'sessions': sessions}


_BOARD_PAGE = """<!doctype html><meta charset="utf-8"><title>后台任务 · kimi-quota-statusline</title>
<style>body{background:#0d1117;color:#c9d1d9;font:14px/1.6 -apple-system,monospace;padding:20px;max-width:960px;margin:auto}
table{border-collapse:collapse;width:100%}td{padding:4px 10px;border-bottom:1px solid #21262d;vertical-align:top}
a{color:#4fa8ff}.muted{color:#8b949e}.run{color:#4fa8ff}.ok{color:#3fb950}.bad{color:#f85149}
h3 span{font-weight:normal;font-size:12px}</style>
<h3>⚙ 后台任务 <span id="meta" class="muted"></span></h3><table id="t"></table>
<script>
function cell(tr, txt, cls) {
  const td = document.createElement('td');
  td.textContent = txt;
  if (cls) td.className = cls;
  tr.appendChild(td);
}
function row(t) {
  const tr = document.createElement('tr');
  const st = t.status || '';
  cell(tr, st, st === 'running' ? 'run' : (st === 'completed' ? 'ok' : 'bad'));
  cell(tr, t.kind || '');
  cell(tr, t.description || t.command || t.taskId);
  const s = Math.max(0, Math.round(((t.endedAt || Date.now()) - (t.startedAt || 0)) / 1000));
  cell(tr, s >= 60 ? Math.floor(s / 60) + 'm' + String(s % 60).padStart(2, '0') + 's' : s + 's');
  const td = document.createElement('td');
  if (t._log_url) {
    const a = document.createElement('a');
    a.href = t._log_url; a.textContent = t.kind === 'agent' ? 'transcript' : 'output';
    td.appendChild(a);
  }
  tr.appendChild(td);
  return tr;
}
async function tick() {
  try {
    const d = await (await fetch('/data')).json();
    const tbl = document.getElementById('t');
    tbl.innerHTML = '';
    let running = 0, total = 0;
    for (const s of d.sessions) {
      const h = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 5; td.className = 'muted';
      td.textContent = '▸ ' + s.label;
      h.appendChild(td); tbl.appendChild(h);
      for (const t of s.tasks) { tbl.appendChild(row(t)); total++; if (t.status === 'running') running++; }
    }
    document.getElementById('meta').textContent =
      running + ' running / ' + total + ' total · ' + new Date().toLocaleTimeString() + ' · 1s 实时';
  } catch (e) {
    document.getElementById('meta').textContent = '连接看板服务失败(可能已闲置自灭),回到终端点 ⚙ 重新拉起';
  }
}
setInterval(tick, 1000); tick();
</script>"""


def _make_tasks_handler():
    from http.server import BaseHTTPRequestHandler

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.last_req = time.time()
            if self.path.startswith('/data'):
                body = json.dumps(tasks_payload(), ensure_ascii=False).encode()
                ctype = 'application/json; charset=utf-8'
            elif self.path.startswith('/log'):
                # 同源回传会话内日志(尾部 64KB):http 页面禁止跳 file://,只能由服务代读。
                # 路径白名单:必须在 SESSIONS 下且是 .log/.jsonl,防任意文件读取
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                rp = os.path.realpath(q.get('p', [''])[0])
                root = os.path.realpath(SESSIONS)
                if rp.startswith(root + os.sep) and rp.endswith(('.log', '.jsonl')):
                    if os.path.isfile(rp):
                        with open(rp, 'rb') as f:
                            f.seek(0, 2)
                            size = f.tell()
                            f.seek(max(0, size - 65536))
                            body = f.read()
                    else:
                        body = '(日志尚未产生,任务可能还在静默运行)'.encode()
                    ctype = 'text/plain; charset=utf-8'
                else:
                    self.send_error(403)
                    return
            elif self.path in ('/', '/index.html'):
                body = _BOARD_PAGE.encode()
                ctype = 'text/html; charset=utf-8'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    return H


def start_tasks_server(port=None, background=True):
    """起看板服务(仅 127.0.0.1);port=None 依次试 TASKS_PORTS,port=0 系统分配(测试)。返回 (server, port)。"""
    from http.server import ThreadingHTTPServer
    candidates = [port] if port is not None else list(TASKS_PORTS)
    srv = None
    for p in candidates:
        try:
            srv = ThreadingHTTPServer(('127.0.0.1', p), _make_tasks_handler())
            break
        except OSError:
            continue
    if srv is None:
        return None, 0
    srv.last_req = time.time()
    srv.daemon_threads = True
    if background:
        import threading
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def stop_tasks_server(srv):
    """停掉后台线程里的服务(测试用;watchdog 自灭走 serve_forever 返回后的 server_close)。"""
    try:
        srv.shutdown()
        srv.server_close()
    except Exception:
        pass


def _probe_port(port, timeout=0.1):
    import socket
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=timeout):
            return True
    except OSError:
        return False


def board_url():
    """看板地址:服务在线返回 http URL;不在线则拉起(detached)并返回 None——下一秒渲染自然接上,
    本次回退静态 file:// 板。渲染预算 300ms,绝不在这等服务就绪。"""
    try:
        port = int(open(TASKS_PORT_FILE).read().split()[0])
    except Exception:
        port = None
    if port and _probe_port(port):
        return f'http://127.0.0.1:{port}/'
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), '--tasks-server'],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **_detached_kwargs())
    except OSError:
        pass
    return None


def tasks_server_main():
    """--tasks-server 入口:选端口、写 port 文件、看门狗(闲置/超限自灭)、阻塞服务。"""
    srv, port = start_tasks_server(port=None, background=False)
    if srv is None:
        return
    t0 = time.time()
    try:
        with open(TASKS_PORT_FILE, 'w') as f:
            f.write(f'{port} {os.getpid()}')
    except OSError:
        pass

    def watchdog():
        import threading  # noqa: F401
        while True:
            time.sleep(30)
            idle = time.time() - srv.last_req > SERVER_IDLE_S
            has_running = any(t.get('status') == 'running'
                              for s in tasks_payload()['sessions'] for t in s['tasks'])
            if (idle and not has_running) or time.time() - t0 > SERVER_MAX_AGE_S:
                srv.shutdown()
                return

    import threading
    threading.Thread(target=watchdog, daemon=True).start()
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
        try:
            os.remove(TASKS_PORT_FILE)
        except OSError:
            pass


def pick(d, *keys, default=''):
    for k in keys:
        v = d.get(k)
        if v not in (None, ''):
            return v
    return default


def main():
    # Windows 控制台 stdio 默认 locale 编码(cp1252/GBK):输入绕过文本层按 UTF-8 解,
    # 输出强制 UTF-8——否则中文目录名一 print 就 UnicodeEncodeError,整行回退内置布局
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stdin, 'buffer'):
        raw = sys.stdin.buffer.read().decode('utf-8', 'replace')
    else:
        raw = sys.stdin.read()
    snap = {}
    try:
        snap = json.loads(raw) if raw.strip() else {}
    except ValueError:
        pass
    try:
        with open(DEBUG_STDIN, 'w', encoding='utf-8', errors='replace') as f:
            f.write(raw[:4096])
    except Exception:
        pass

    sid = pick(snap, 'sessionId', 'session_id')
    tokens = load_tokens(sid, str(snap.get('version') or ''))
    line1 = []

    # 权限模式最前(大写)
    mode = pick(snap, 'permissionMode', 'permission_mode')
    if mode:
        mc = DIM if mode == 'manual' else (YELLOW if mode == 'yolo' else RED)
        parts_mode = c(str(mode).upper(), mc, BOLD if mode != 'manual' else DIM)
        line1.append(parts_mode)

    # 模型(青)·强度(暗) [上下文规格]
    model = pick(snap, 'model', 'model_alias', 'modelAlias')
    if isinstance(model, dict):
        model = pick(model, 'alias', 'id', 'name')
    effort, swarm, enter_ts = session_state(sid)
    if model:
        seg = c(str(model).split('/')[-1], CYAN, BOLD) + (c('·' + effort, DIM) if effort else '')
        max_ctx = pick(snap, 'maxContextTokens', 'max_context_tokens', default=0) or 0
        if max_ctx:
            seg += ' ' + c(f'[{fmt_ctx(max_ctx)}]', DIM)
        line1.append(seg)

    # swarm 静态标记(品牌蓝);进入瞬间的扫描动效在输出阶段处理
    if swarm:
        line1.append(brand_fg('swarm', BRAND, BOLD))

    # 后台任务段:当前会话有 running 的任务/子 agent 才显示;点击打开本地看板
    tseg = task_segment(session_tasks(sid))
    if tseg:
        line1.append(tseg)

    # 上下文条:原生 UI(line 2)已有,这里不重复

    # 5h / 7d 额度条:只用官方数据(本地折算与官方窗口非线性,校准漂移曾致 5h 误显 90%+,已弃用);
    # 超过 OFFICIAL_FRESH_S 未更新则压暗并加 ~ 过期标记;从未拉到官方数据则不显示,不瞎猜
    if tokens:
        off = tokens.get('official') or {}
        stale = not off or (time.time() - off.get('ts', 0)) > OFFICIAL_FRESH_S
        for label, okey in (('5h', 'h5'), ('7d', 'wk')):
            if not off.get(f'{okey}_limit'):
                continue
            ratio = min(1.0, off[f'{okey}_used'] / off[f'{okey}_limit'])
            filled = min(6, max(0, round(ratio * 6)))
            color = GREEN if ratio < 0.6 else (YELLOW if ratio < 0.85 else RED)
            if stale:
                bar = c('█' * filled, DIM) + c('░' * (6 - filled), DIM)
                seg = c(f'{label} ', DIM) + bar + ' ' + c(f'~{round(100 * ratio)}%', DIM)
            else:
                bar = c('█' * filled, color) + c('░' * (6 - filled), DIM)
                seg = c(f'{label} ', DIM) + bar + ' ' + c(f'{round(100 * ratio)}%', color, BOLD)
            hint = reset_hint(off.get(f'{okey}_reset', ''))
            if hint:
                seg += c(f' {hint}', DIM)
            line1.append(seg)

    if snap.get('planMode') or snap.get('plan_mode'):
        line1.append(c('plan', BLUE, BOLD))

    git = pick(snap, 'gitBranch', 'git_branch', 'branch')
    if isinstance(git, dict):
        git = pick(git, 'branch', 'name')
    if git:
        git = str(git)
        if len(git) > 24:  # 分支名过长会撑爆状态栏,截断保留头部
            git = git[:23] + '…'
        line1.append(c(f'⎇ {git}', GREEN))

    # 本会话 token + 金额(按官方定价) + 实时 TPS(最近几次请求均值,空闲保留最后值) + 项目目录
    sess = pick_sess(tokens, sid)
    if sess:
        seg = c(fmt_tokens(sess.get('tokens', 0)), YELLOW)
        cost = sess.get('cost')
        if cost is not None:
            seg += ' ' + c(f'¥{cost:.2f}', YELLOW, BOLD)
        tps = live_tps(sid) or session_tps(sess)
        if tps > 0:
            tps_txt = f'{tps:.1f}' if tps < 100 else fmt_tokens(int(tps))
            seg += ' ' + c(f'{tps_txt}t/s', MAGENTA)
        line1.append(seg)
    cwd = pick(snap, 'cwd', 'work_dir', 'workDir')
    if cwd:
        d = os.path.basename(str(cwd).rstrip('/'))
        if len(d) > 20:
            d = d[:19] + '…'
        line1.append(c(d, BLUE))

    out = sep().join(line1) if line1 else 'kimi-code'
    elapsed = time.time() - enter_ts if (swarm and enter_ts) else 1e9
    if swarm and USE_ANSI and elapsed < BURST_S:
        # 进入 swarm 的前几秒:品牌蓝水波自 swarm 处向两侧荡开,随后收敛为普通分段色
        # (OSC 8 超链接也要剥掉,否则当可见字符参与水波计算会把动画冲乱)
        print(brand_flow(OSC_RE.sub('', ANSI_RE.sub('', out)), elapsed))
    else:
        print(out)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--refresh':
        refresh_cache(sys.argv[2] if len(sys.argv) > 2 else '',
                      sys.argv[3] if len(sys.argv) > 3 else '')
    elif len(sys.argv) > 1 and sys.argv[1] == '--tasks-server':
        tasks_server_main()
    else:
        main()
