#!/usr/bin/env python3
"""回归测试(无框架,直接 python3 tests/test_regressions.py):

1. 5h/7d 额度条只显示官方数据:过期加 ~ 压暗、本地折算不得再混入、无官方数据不显示
   (2026-08-09 修复:本地 token 折算与官方窗口非线性,校准漂移导致 5h 误显 90%+)
2. 长会话 wire.jsonl 中 swarm_mode.enter 跌出尾部 512K 窗口后仍能识别 swarm 状态
   (2026-08-09 修复:session_state 改为自文件尾向前分块扫描)
"""
import io
import json
import os
import sys
import tempfile
import time
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ['KIMI_SL_NOCOLOR'] = '1'  # 纯文本,便于断言(须在 import 前设置)
import statusline

# Windows 控制台 stdout 默认 locale 编码(cp1252/GBK),中文用例名会炸;统一按 UTF-8 输出
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

FAILED = []


def check(name, cond):
    print(('PASS' if cond else 'FAIL'), name)
    if not cond:
        FAILED.append(name)


def render(cache_obj, sid=''):
    """用指定缓存渲染一次状态栏,返回输出文本。"""
    fd, path = tempfile.mkstemp(suffix='.json')
    with os.fdopen(fd, 'w') as f:
        json.dump(cache_obj, f)
    old_cache, old_stdin = statusline.CACHE, sys.stdin
    statusline.CACHE = path
    sys.stdin = io.StringIO(json.dumps({'sessionId': sid, 'version': 'test'}))
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            statusline.main()
    finally:
        statusline.CACHE = old_cache
        sys.stdin = old_stdin
        os.unlink(path)
    return buf.getvalue()


def make_wire(size_kb, events):
    """造一个 wire.jsonl:events 在前,后面垫 noise 到 size_kb。返回 (sid, 文件路径)。"""
    tmp = tempfile.mkdtemp()
    statusline.SESSIONS = tmp
    sid = 'sess_test'
    d = os.path.join(tmp, 'wd_t', sid, 'agents', 'main')
    os.makedirs(d)
    p = os.path.join(d, 'wire.jsonl')
    pad = json.dumps({'type': 'noise', 'pad': 'x' * 1000}) + '\n'
    with open(p, 'w') as f:
        for ev in events:
            f.write(json.dumps(ev) + '\n')
        while os.path.getsize(p) < size_kb * 1024:
            f.write(pad)
    return sid, p


now = time.time()

# ---- Bug 1:额度条口径 ----
out = render({'ts': now, 'official': {'ts': now, 'h5_used': 4, 'h5_limit': 100,
                                      'wk_used': 27, 'wk_limit': 100}})
check('官方数据新鲜:显示 4%%,不带 ~', '4%' in out and '~4%' not in out)

out = render({'ts': now, 'h5': 44112427, 'd7': 152168064,
              'official': {'ts': now - 3600, 'h5_used': 4, 'h5_limit': 100,
                           'wk_used': 27, 'wk_limit': 100}})
check('官方数据过期:显示 ~4%% 过期标记', '~4%' in out)
check('官方数据过期:本地折算 93%% 不得混入', '93%' not in out)

out = render({'ts': now, 'h5': 44112427, 'd7': 152168064})
check('无官方数据:不显示 5h/7d 条', '5h' not in out and '7d' not in out)

# ---- Bug 2:长会话 swarm 识别 ----
enter = {'type': 'swarm_mode.enter', 'time': int(now * 1000)}
sid, _ = make_wire(600, [enter])
check('enter 跌出尾部 512K(600K 文件):仍识别 swarm', statusline.session_state(sid)[1] is True)

sid, _ = make_wire(10, [enter])
check('小文件 enter 在尾部:识别 swarm', statusline.session_state(sid)[1] is True)

sid, p = make_wire(600, [enter])
with open(p, 'a') as f:
    f.write(json.dumps({'type': 'swarm_mode.exit', 'time': int(now * 1000)}) + '\n')
check('enter 后已 exit:不显示 swarm', statusline.session_state(sid)[1] is False)

# 同一块内多条 swarm 记录:后者胜出(2026-08-09 曾误用行级门控,导致块内首条胜出)
exit_ = {'type': 'swarm_mode.exit', 'time': int(now * 1000)}
sid, _ = make_wire(10, [enter, exit_])
check('同块 [enter, exit]:不显示 swarm', statusline.session_state(sid)[1] is False)
sid, _ = make_wire(10, [enter, exit_, enter])
check('同块 [enter, exit, enter]:显示 swarm', statusline.session_state(sid)[1] is True)

# ---- TPS:会话平均 output ÷ 活跃时长(首条→末条记录),常驻不隐藏 ----
tmp_tps = tempfile.mkdtemp()
statusline.SESSIONS = tmp_tps
sid_t = 'sess_tps'
d_t = os.path.join(tmp_tps, 'wd_t', sid_t, 'agents', 'main')
os.makedirs(d_t)


def rec_t(t_ms, out, inp=0):
    return json.dumps({'type': 'usage.record',
                       'usage': {'inputOther': inp, 'output': out,
                                 'inputCacheRead': inp, 'inputCacheCreation': 0},
                       'time': t_ms})


now_ms = int(time.time() * 1000)
with open(os.path.join(d_t, 'wire.jsonl'), 'w') as f:
    f.write('\n'.join([rec_t(now_ms - 100000, 500, inp=100000),  # 首条,活跃时长起点
                       rec_t(now_ms - 50000, 300, inp=100000),
                       rec_t(now_ms, 200, inp=100000)])          # 末条,output 合计 1000
          + '\n')

# refresh_cache 全量扫描时沉淀 out/t0/t1(屏蔽网络与真实缓存路径)
_old_fetch, _old_cache, _old_lock = (statusline.fetch_official,
                                     statusline.CACHE, statusline.LOCK)
statusline.fetch_official = lambda ver='': None
statusline.CACHE = os.path.join(tmp_tps, 'cache.json')
statusline.LOCK = os.path.join(tmp_tps, 'lock')
try:
    statusline.refresh_cache(sid_t, 'test')
    _sess = json.load(open(statusline.CACHE))['sessions'][sid_t]
finally:
    statusline.fetch_official, statusline.CACHE, statusline.LOCK = _old_fetch, _old_cache, _old_lock

check('refresh_cache:沉淀 out/t0/t1(input/cache 不计入 out)',
      _sess['out'] == 1000 and _sess['t0'] == now_ms - 100000 and _sess['t1'] == now_ms)
# 活跃时长 100s、output 1000 → 10/s;input/cache 各 30 万不得混入(混入则 ~3010/s)
check('session_tps:output÷活跃时长=1000/100', statusline.session_tps(_sess) == 1000 / 100)
check('session_tps:不足 2 条记录(t0==t1)返回 0',
      statusline.session_tps({'out': 500, 't0': 1000, 't1': 1000}) == 0.0)
check('session_tps:空 sess/缺字段返回 0', statusline.session_tps({}) == 0.0)


# ---- live_tps:最近 3 次「llm.request→usage.record」配对的 output÷耗时均值 ----
def req_t(t_ms):
    return json.dumps({'type': 'llm.request', 'time': t_ms})


def wire_pairs(pairs):
    """pairs: [(req_offset_ms, dur_ms, out), ...] 时间序 → 写入 wire.jsonl"""
    with open(os.path.join(d_t, 'wire.jsonl'), 'w') as f:
        for off, dur, out in pairs:
            f.write(req_t(now_ms + off) + '\n')
            f.write(rec_t(now_ms + off + dur, out) + '\n')


wire_pairs([(-100000, 10000, 300),   # 30/s(最老,超出 pair_n=3 时不计入)
            (-80000, 10000, 400),    # 40/s
            (-60000, 10000, 500),    # 50/s
            (-40000, 10000, 600)])   # 60/s
# 最近 3 对均值 = (40+50+60)/3 = 50;最老的 30/s 不计
check('live_tps:最近 3 次配对均值=(40+50+60)/3', statusline.live_tps(sid_t) == 50.0)
check('live_tps:未知会话返回 0', statusline.live_tps('sess_none') == 0.0)
check('live_tps:空 sessionId 返回 0', statusline.live_tps('') == 0.0)
# 只有请求没有 usage.record(在途/取消):无配对返回 0
with open(os.path.join(d_t, 'wire.jsonl'), 'w') as f:
    f.write(req_t(now_ms - 1000) + '\n')
check('live_tps:只有在途请求返回 0', statusline.live_tps(sid_t) == 0.0)
# 耗时 >600s 的配对不合理(请求与记录错配),丢弃
wire_pairs([(-700000, 601000, 9999), (-10000, 5000, 200)])  # 40/s 生效
check('live_tps:耗时>600s 的错配丢弃', statusline.live_tps(sid_t) == 40.0)
# 只有 usage.record 没有 llm.request:扫满 4 块也无配对,返回 0
with open(os.path.join(d_t, 'wire.jsonl'), 'w') as f:
    f.write(rec_t(now_ms - 1000, 500) + '\n')
check('live_tps:只有 usage.record 返回 0', statusline.live_tps(sid_t) == 0.0)


# 分块扫描机制:配对垫出尾部 512K 仍能凑够;超过 4 块(2MB)上限优雅截断
def build_wire_padded(tail_lines, pad_kb, head_lines=None):
    p = os.path.join(d_t, 'wire.jsonl')
    pad = json.dumps({'type': 'noise', 'pad': 'x' * 1000}) + '\n'
    with open(p, 'w') as f:
        for l in (head_lines or []):
            f.write(l + '\n')
        base = f.tell()
        while f.tell() < base + pad_kb * 1024:
            f.write(pad)
        for l in tail_lines:
            f.write(l + '\n')


# 最老一对(30/s)被 600K 噪声垫出尾部块,须翻块才能凑够 3 对
build_wire_padded(
    [req_t(now_ms - 20000), rec_t(now_ms - 15000, 250),   # 50/s
     req_t(now_ms - 10000), rec_t(now_ms, 400)],          # 40/s
    600,
    head_lines=[req_t(now_ms - 100000), rec_t(now_ms - 90000, 300)])  # 30/s
check('live_tps:配对跌出尾部 512K 翻块凑够 3 对(40+50+30)/3',
      statusline.live_tps(sid_t) == 40.0)
# 可及范围内只有 1 对(更早的在 2.5MB 之外,超 4 块上限):返回少对均值而非错值
build_wire_padded(
    [req_t(now_ms - 10000), rec_t(now_ms, 400)],          # 40/s
    2500,
    head_lines=[req_t(now_ms - 100000), rec_t(now_ms - 90000, 9999)])
check('live_tps:超 4 块上限优雅截断(仅 1 对=40/s)', statusline.live_tps(sid_t) == 40.0)

# ---- Windows 适配(v1.2.0):detached 参数 / stdin UTF-8 / 跨平台安装器 ----
# A. detached 刷新进程的 Popen 参数按平台分支(Windows 用 DETACHED_PROCESS 防闪控制台窗口)
dk = statusline._detached_kwargs('posix') if hasattr(statusline, '_detached_kwargs') else None
check('posix:detached 用 start_new_session', dk == {'start_new_session': True})
dk = statusline._detached_kwargs('nt') if hasattr(statusline, '_detached_kwargs') else None
check('nt:detached 用 creationflags(DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP),无 start_new_session',
      isinstance(dk, dict) and 'start_new_session' not in dk
      and (dk.get('creationflags', 0) & 0x8) != 0 and (dk.get('creationflags', 0) & 0x200) != 0)


def render_bytes(stdin_bytes, cache_obj):
    """stdin 文本层故意用 ascii(模拟 Windows 非 UTF-8 locale):直接 .read() 必炸,正解是读 .buffer 按 UTF-8 解。"""
    fd, path = tempfile.mkstemp(suffix='.json')
    with os.fdopen(fd, 'w') as f:
        json.dump(cache_obj, f)
    old_cache, old_stdin, old_dbg = statusline.CACHE, sys.stdin, statusline.DEBUG_STDIN
    dbg = os.path.join(tempfile.mkdtemp(), 'stdin.json')
    statusline.CACHE, statusline.DEBUG_STDIN = path, dbg
    sys.stdin = io.TextIOWrapper(io.BytesIO(stdin_bytes), encoding='ascii')
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            statusline.main()
    except Exception:
        pass
    finally:
        statusline.CACHE, statusline.DEBUG_STDIN = old_cache, old_dbg
        sys.stdin = old_stdin
        os.unlink(path)
    return buf.getvalue()


# B. stdin 文本层编码错误时仍按 UTF-8 解析;debug 快照写入也不得炸(中文路径)
snap_bytes = json.dumps({'sessionId': '', 'version': 't', 'cwd': 'D:/用户/项目'},
                        ensure_ascii=False).encode('utf-8')
out = render_bytes(snap_bytes, {'ts': now})
check('stdin 编码错误 locale:UTF-8 中文目录名正常显示', '项目' in out)

# D. stdout 是非 UTF-8 locale(模拟 Windows en-US 控制台 cp1252):输出强制 UTF-8,
#    否则中文目录名直接 UnicodeEncodeError,状态栏整行回退内置布局
fd, path = tempfile.mkstemp(suffix='.json')
with os.fdopen(fd, 'w') as f:
    json.dump({'ts': now}, f)
old_cache, old_stdin, old_stdout = statusline.CACHE, sys.stdin, sys.stdout
old_dbg = statusline.DEBUG_STDIN
statusline.CACHE = path
statusline.DEBUG_STDIN = os.path.join(tempfile.mkdtemp(), 'stdin.json')
sys.stdin = io.TextIOWrapper(io.BytesIO(snap_bytes), encoding='utf-8')
raw_out = io.BytesIO()
sys.stdout = io.TextIOWrapper(raw_out, encoding='cp1252')
try:
    statusline.main()
    sys.stdout.flush()
    cond = '项目'.encode('utf-8') in raw_out.getvalue()
except Exception:
    cond = False
finally:
    statusline.CACHE, statusline.DEBUG_STDIN = old_cache, old_dbg
    sys.stdin, sys.stdout = old_stdin, old_stdout
    os.unlink(path)
check('stdout 非 UTF-8 locale:输出强制 UTF-8(中文目录名不乱码)', cond)

# C. 跨平台安装器 install.py / uninstall.py
try:
    import install as _inst
    import uninstall as _uninst
except ImportError:
    _inst = _uninst = None
check('install.py / uninstall.py 可导入', _inst is not None and _uninst is not None)

if _inst:
    line_posix = _inst.build_command('/p/kimi-quota-statusline/statusline.py', os_name='posix')
    check('posix 命令行:python3 + 路径',
          line_posix == 'command = "python3 /p/kimi-quota-statusline/statusline.py"')
    line_nt = _inst.build_command('C:\\p\\kimi-quota-statusline\\statusline.py',
                                  os_name='nt', executable='C:\\Py\\python.exe')
    check('nt 命令行(无空格/元字符):解释器绝对路径 + TOML 转义,不加引号',
          line_nt == 'command = "C:\\\\Py\\\\python.exe C:\\\\p\\\\kimi-quota-statusline\\\\statusline.py"')

    # posix 安装 → 幂等 → 卸载往返
    home = tempfile.mkdtemp()
    tui = os.path.join(home, 'tui.toml')
    target = os.path.join(home, 'plugins', 'kimi-quota-statusline', 'statusline.py')
    os.makedirs(os.path.dirname(target))
    open(target, 'w').close()
    rc = _inst.install(tui, target, doctor=False)
    text = open(tui, encoding='utf-8').read()
    check('安装:写入 [status_line] 且 command 指向插件',
          rc == 0 and '[status_line]' in text and 'statusline.py' in text)
    _inst.install(tui, target, doctor=False)
    check('安装幂等:内容不变', open(tui, encoding='utf-8').read() == text)
    rc = _uninst.uninstall(tui, os.path.dirname(target), doctor=False)
    text = open(tui, encoding='utf-8').read()
    check('卸载:command 与空 section 一并移除',
          rc == 0 and 'statusline.py' not in text and '[status_line]' not in text)
    check('卸载生成 .bak 备份', any(f.endswith('.bak') for f in os.listdir(home)))

    # 覆盖已有 command:其他 section 不受影响
    home2 = tempfile.mkdtemp()
    tui2 = os.path.join(home2, 'tui.toml')
    open(tui2, 'w', encoding='utf-8').write('[status_line]\ncommand = "python3 /other/x.py"\n\n[editor]\n')
    _inst.install(tui2, os.path.join(home2, 'statusline.py'), doctor=False)
    text = open(tui2, encoding='utf-8').read()
    check('覆盖已有 command:其他 section 保留',
          'statusline.py' in text and '/other/x.py' not in text and '[editor]' in text)

    # Windows 形态:转义路径写入,卸载也能认出
    home3 = tempfile.mkdtemp()
    tui3 = os.path.join(home3, 'tui.toml')
    tgt3 = 'C:\\kc\\plugins\\kimi-quota-statusline\\statusline.py'
    _inst.install(tui3, tgt3, os_name='nt', executable='C:\\Py\\python.exe', doctor=False)
    check('nt 安装:命令行含转义反斜杠',
          '\\\\' in open(tui3, encoding='utf-8').read().split('command')[1])
    rc = _uninst.uninstall(tui3, 'C:\\kc\\plugins\\kimi-quota-statusline', doctor=False)
    check('nt 卸载:转义路径同样识别并移除',
          rc == 0 and 'statusline.py' not in open(tui3, encoding='utf-8').read())

    # 覆盖已有 command 时走 re.sub 替换路径:Windows 路径的 \\ 不得被替换串转义规则吃掉
    # (2026-08-13 真机事故:re.sub 把 cmd_line 的 \\ 当转义,tui.toml 写成非法 TOML)
    home9 = tempfile.mkdtemp()
    tui9 = os.path.join(home9, 'tui.toml')
    open(tui9, 'w', encoding='utf-8').write('[status_line]\ncommand = "old"\n')
    tgt9 = 'C:\\kc\\plugins\\kimi-quota-statusline\\statusline.py'
    _inst.install(tui9, tgt9, os_name='nt', executable='C:\\Py\\python.exe', doctor=False)
    text9 = open(tui9, encoding='utf-8').read()
    ok9 = '\\\\' in text9  # 双写反斜杠必须活着写进文件
    try:
        import tomllib
        ok9 = ok9 and tomllib.loads(text9)['status_line']['command'] == \
            f'C:\\Py\\python.exe {tgt9}'
    except ImportError:
        pass
    except Exception:
        ok9 = False
    check('nt 覆盖已有 command:写入仍是合法 TOML 且路径还原正确', ok9)

    # 卸载不误伤(code review 发现的退化):section 内注释提及插件路径、command 指向
    # 他人脚本时,command 必须保留——卸载是行级精确匹配,不是 section 级宽松匹配
    home4 = tempfile.mkdtemp()
    tui4 = os.path.join(home4, 'tui.toml')
    pdir4 = os.path.join(home4, 'kimi-quota-statusline')
    open(tui4, 'w', encoding='utf-8').write(
        f'[status_line]\n# was {pdir4}/statusline.py\ncommand = "python3 /other/x.py"\n')
    _uninst.uninstall(tui4, pdir4, doctor=False)
    check('卸载不误伤:注释提及插件但 command 指向他人时保留',
          '/other/x.py' in open(tui4, encoding='utf-8').read())

    # 安装:tui.toml 以无尾换行的裸 [status_line] 结尾时不得产出重复 section(非法 TOML)
    home5 = tempfile.mkdtemp()
    tui5 = os.path.join(home5, 'tui.toml')
    open(tui5, 'w', encoding='utf-8').write('[editor]\ntheme = "x"\n\n[status_line]')
    _inst.install(tui5, os.path.join(home5, 'statusline.py'), doctor=False)
    text5 = open(tui5, encoding='utf-8').read()
    check('裸 section 头(无尾换行):不重复追加 section',
          text5.count('[status_line]') == 1 and 'statusline.py' in text5)

    # 幂等收窄:插件路径只出现在其他 section 的注释里、command 指向他人时,必须照常安装
    home6 = tempfile.mkdtemp()
    tui6 = os.path.join(home6, 'tui.toml')
    tgt6 = os.path.join(home6, 'kimi-quota-statusline', 'statusline.py')
    open(tui6, 'w', encoding='utf-8').write(
        f'[status_line]\ncommand = "python3 /other/x.py"\n\n[editor]\n# see {tgt6}\n')
    _inst.install(tui6, tgt6, doctor=False)
    text6 = open(tui6, encoding='utf-8').read()
    check('幂等收窄:路径仅在他处注释时仍正常安装',
          '/other/x.py' not in text6 and tgt6 in text6)

    # CRLF 换行的 tui.toml(Windows 记事本编辑过)也能识别已有 section
    home7 = tempfile.mkdtemp()
    tui7 = os.path.join(home7, 'tui.toml')
    open(tui7, 'w', encoding='utf-8', newline='').write(
        '[status_line]\r\ncommand = "python3 /other/x.py"\r\n')
    _inst.install(tui7, os.path.join(home7, 'statusline.py'), doctor=False)
    check('CRLF:识别已有 section 不重复追加',
          open(tui7, encoding='utf-8').read().count('[status_line]') == 1)

    # nt 中文+空格混合路径:转义正确(CI 三平台路径全 ASCII,锁死这个组合)
    line_mix = _inst.build_command('C:\\用户\\我的项目\\kimi-quota-statusline\\statusline.py',
                                   os_name='nt', executable='C:\\程序 Files\\python.exe')
    check('nt 中文+空格路径:转义正确',
          line_mix == 'command = "\\"C:\\\\程序 Files\\\\python.exe\\" '
                      '\\"C:\\\\用户\\\\我的项目\\\\kimi-quota-statusline\\\\statusline.py\\""')

    # cmd 把 , ; = 也当参数分隔符(C:\a,b\x 会被切碎),必须退回引号形态
    line_sep = _inst.build_command('C:\\a,b\\kimi-quota-statusline\\statusline.py',
                                   os_name='nt', executable='C:\\Py\\python.exe')
    check('nt 路径含 , ; = 等 cmd 分隔符:退回引号形态',
          line_sep == 'command = "\\"C:\\\\Py\\\\python.exe\\" '
                      '\\"C:\\\\a,b\\\\kimi-quota-statusline\\\\statusline.py\\""')

    # Windows 特有坑:kimi 是 npm 生成的 kimi.cmd shim 时 CreateProcess 抛 WinError 193
    # (OSError)——doctor 失败只提示不阻断,配置已写入不得判安装失败(回归锁死)
    home8 = tempfile.mkdtemp()
    tui8 = os.path.join(home8, 'tui.toml')
    tgt8 = os.path.join(home8, 'statusline.py')
    open(tgt8, 'w').close()
    saved_run, saved_which = _inst.subprocess.run, _inst.shutil.which
    _inst.shutil.which = lambda *a, **k: 'C:/npm/kimi.CMD'

    def _boom(*a, **k):
        raise OSError(193, 'not a valid Win32 application')

    _inst.subprocess.run = _boom
    try:
        rc = _inst.install(tui8, tgt8, doctor=True)
        check('doctor OSError(kimi.cmd shim):安装不阻断且返回 0',
              rc == 0 and 'statusline.py' in open(tui8, encoding='utf-8').read())
    finally:
        _inst.subprocess.run, _inst.shutil.which = saved_run, saved_which

    saved_run2, saved_which2 = _uninst.subprocess.run, _uninst.shutil.which
    _uninst.shutil.which = lambda *a, **k: 'C:/npm/kimi.CMD'
    _uninst.subprocess.run = _boom
    try:
        rc = _uninst.uninstall(tui8, home8, doctor=True)
        check('doctor OSError(kimi.cmd shim):卸载不阻断且返回 0',
              rc == 0 and 'statusline.py' not in open(tui8, encoding='utf-8').read())
    finally:
        _uninst.subprocess.run, _uninst.shutil.which = saved_run2, saved_which2

# ---- 多会话缓存互踩:单 sess 槽位被后刷新的会话覆盖,另一窗口的
# token/金额/TPS 段随对端刷新消失(2026-08-16 双窗口真机复现) ----
tmp_ms = tempfile.mkdtemp()
statusline.SESSIONS = tmp_ms


def make_sess_wire(sid, out_tokens, t1_ms):
    d = os.path.join(tmp_ms, 'wd_m', sid, 'agents', 'main')
    os.makedirs(d)
    with open(os.path.join(d, 'wire.jsonl'), 'w') as f:
        f.write(json.dumps({'type': 'usage.record',
                            'usage': {'inputOther': 0, 'output': out_tokens,
                                      'inputCacheRead': 0, 'inputCacheCreation': 0},
                            'time': t1_ms}) + '\n')


sid_a, sid_b = 'sess_multi_a', 'sess_multi_b'
make_sess_wire(sid_a, 1000, now_ms - 1000)
make_sess_wire(sid_b, 2000, now_ms)

_old_fetch, _old_cache, _old_lock = (statusline.fetch_official,
                                     statusline.CACHE, statusline.LOCK)
statusline.fetch_official = lambda ver='': None
statusline.CACHE = os.path.join(tmp_ms, 'cache.json')
statusline.LOCK = os.path.join(tmp_ms, 'lock')
try:
    statusline.refresh_cache(sid_a, 'test')
    statusline.refresh_cache(sid_b, 'test')  # B 后刷,不得挤掉 A 的条目
    _cache = json.load(open(statusline.CACHE))
    out_a = render(_cache, sid_a)
    out_b = render(_cache, sid_b)
    check('多会话:A 窗口在 B 刷新后仍显示自己的 token 段', '1.0K' in out_a)
    check('多会话:B 窗口显示自己的 token 段', '2.0K' in out_b)

    # 无 wire 的 sid 触发刷新:不得清空其他会话的条目
    statusline.refresh_cache('sess_gone', 'test')
    _cache2 = json.load(open(statusline.CACHE))
    out_a2 = render(_cache2, sid_a)
    check('多会话:无关会话刷新后 A 段不丢', '1.0K' in out_a2)

    # 裁剪:条目按最近活跃(t1)保留至多 8 个,最老的丢弃
    for i in range(9):
        s = f'sess_prune_{i}'
        make_sess_wire(s, 10 + i, now_ms - (9 - i) * 1000)
        statusline.refresh_cache(s, 'test')
    _sessions = json.load(open(statusline.CACHE)).get('sessions') or {}
    check('会话映射存在且裁剪至 8 条', len(_sessions) == 8)
    check('裁剪丢最老条目', 'sess_prune_0' not in _sessions and 'sess_prune_1' not in _sessions)
finally:
    statusline.fetch_official, statusline.CACHE, statusline.LOCK = _old_fetch, _old_cache, _old_lock

# 旧版单槽位缓存:渲染端读兼容(迁移完成前老缓存仍能显示)
out_legacy = render({'ts': now, 'sess': {'id': 'sess_old', 'tokens': 5000, 'cost': 1.0,
                                         'out': 0, 't0': None, 't1': None}}, 'sess_old')
check('旧版缓存 schema:渲染兼容单槽位', '5.0K' in out_legacy)

# ---------- 双 OAuth(0.38.0+):logout/login 切环境后,凭据与端点跟随 config.toml ----------
# 事故原型:凭据写死 kimi-code.json 老槽位,国际版登录后凭据在 kimi-code-env-<hash>.json,
# fetch_official 读不到 → 一直显示缓存里旧账号额度
tmp_dual = tempfile.mkdtemp()
_cfg = os.path.join(tmp_dual, 'config.toml')
with open(_cfg, 'w') as f:
    f.write('[providers."managed:kimi-code"]\n'
            'base_url = "https://api.kimi.ai/coding/v1"\n\n'
            '[providers."managed:kimi-code".oauth]\n'
            'key = "oauth/kimi-code-env-abc123"\n')
_cred_dir = os.path.join(tmp_dual, 'credentials')
os.mkdir(_cred_dir)
json.dump({'access_token': 'TOK_INTL', 'expires_at': time.time() + 600},
          open(os.path.join(_cred_dir, 'kimi-code-env-abc123.json'), 'w'))
json.dump({'access_token': 'TOK_LEGACY', 'expires_at': time.time() + 600},
          open(os.path.join(_cred_dir, 'kimi-code.json'), 'w'))  # 老槽位也在,证明不读错

_old_cfg, _old_cdir = statusline.CONFIG_FILE, statusline.CRED_DIR
statusline.CONFIG_FILE, statusline.CRED_DIR = _cfg, _cred_dir
_seen = {}
import urllib.request as _ur
class _Resp:
    def read(self):
        return json.dumps({'usage': {'limit': '100', 'used': '5', 'resetTime': 'x'},
                           'limits': [{'window': {'duration': 300, 'timeUnit': 'TIME_UNIT_MINUTE'},
                                       'detail': {'limit': '100', 'used': '23', 'resetTime': 'y'}}]}).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False
def _fake_urlopen(req, timeout=0):
    _seen['url'] = req.full_url
    _seen['auth'] = req.headers.get('Authorization')
    return _Resp()
_old_urlopen = _ur.urlopen
_ur.urlopen = _fake_urlopen
try:
    _off = statusline.fetch_official('test')
finally:
    _ur.urlopen = _old_urlopen
    statusline.CONFIG_FILE, statusline.CRED_DIR = _old_cfg, _old_cdir
check('双 OAuth:跟随 config.toml 选中国际版端点', _seen.get('url') == 'https://api.kimi.ai/coding/v1/usages')
check('双 OAuth:读 env 凭据而非老槽位', _seen.get('auth') == 'Bearer TOK_INTL')
check('双 OAuth:额度解析正常', bool(_off) and _off.get('h5_used') == 23.0 and _off.get('wk_used') == 5.0)

# 无 config.toml(老版本 CLI)回退国内默认槽位
_old_cfg = statusline.CONFIG_FILE
statusline.CONFIG_FILE = os.path.join(tmp_dual, '不存在.toml')
try:
    _cred, _url = statusline.resolve_official_endpoint()
finally:
    statusline.CONFIG_FILE = _old_cfg
check('双 OAuth:缺配置回退老凭据+官方域名',
      _cred.endswith(os.path.join('credentials', 'kimi-code.json'))
      and _url == 'https://api.kimi.com/coding/v1/usages')

# ---------- 后台任务段:当前会话有 running 任务/子 agent 才显示,⚙ N 可点击打开看板 ----------
tmp_bg = tempfile.mkdtemp()
statusline.SESSIONS = tmp_bg
statusline.TASKS_HTML = os.path.join(tmp_bg, 'board.html')
statusline.board_url = lambda: None  # 强制 file:// 回退路径,与本机真实看板服务隔离
sid_bg = 'sess_bg'
_tdir = os.path.join(tmp_bg, 'wd_t', sid_bg, 'agents', 'main', 'tasks')
os.makedirs(_tdir)


def _mk_task(tid, status, desc, started=1000, ended=None, kind='process'):
    t = {'taskId': tid, 'status': status, 'description': desc, 'kind': kind,
         'startedAt': started, 'detached': True}
    if ended:
        t['endedAt'] = ended
    json.dump(t, open(os.path.join(_tdir, tid + '.json'), 'w'))
    os.makedirs(os.path.join(_tdir, tid), exist_ok=True)
    open(os.path.join(_tdir, tid, 'output.log'), 'w').write('log of ' + tid)


_mk_task('bash-run1', 'running', '跑着呢 1', started=int(now * 1000) - 60000)
_mk_task('agent-run2', 'running', '跑着呢 2', started=int(now * 1000) - 30000, kind='agent')
_mk_task('bash-done3', 'completed', '已完工', started=1000, ended=2000)

out = render({'ts': now}, sid_bg)
check('后台任务:有 running 时显示计数(2 跑 1 完)', '⚙ 2' in out)
check('后台任务:段带 OSC 8 看板链接', ']8;;file://' in out and 'board.html' in out)
_board = open(statusline.TASKS_HTML, encoding='utf-8').read()
check('后台任务:看板含任务描述与 output 链接',
      '跑着呢 1' in _board and '已完工' in _board and 'output.log' in _board)

# 全部结束后段隐藏
import shutil
shutil.rmtree(_tdir)
os.makedirs(_tdir)
_mk_task('bash-done4', 'completed', '旧任务', started=1000, ended=2000)
out = render({'ts': now}, sid_bg)
check('后台任务:无 running 时段隐藏', '⚙' not in out)

# swarm 动效通道:OSC 8 必须剥除(否则水波把超链接序列当可见字符,动画被冲乱)
check('后台任务:OSC 8 剥除后只剩可见文字',
      statusline.OSC_RE.sub('', '\033]8;;file://x\a⚙ 2\033]8;;\a') == '⚙ 2')

# ---------- 实时任务看板服务(v1.5.0):全会话总览 + 1s 局部刷新 ----------
tmp_srv = tempfile.mkdtemp()
statusline.SESSIONS = tmp_srv


def _mk_sess_task(wd, sid, tid, status, desc, started, ended=None):
    td = os.path.join(tmp_srv, wd, sid, 'agents', 'main', 'tasks')
    os.makedirs(td, exist_ok=True)
    t = {'taskId': tid, 'status': status, 'description': desc, 'kind': 'process',
         'startedAt': started, 'detached': True}
    if ended:
        t['endedAt'] = ended
    json.dump(t, open(os.path.join(td, tid + '.json'), 'w'))


_mk_sess_task('wd_projA_aaa111', 'sess_A', 'bash-a1', 'completed', 'A 的旧任务', 1000, 2000)
_mk_sess_task('wd_projA_aaa111', 'sess_A', 'bash-a2', 'running', 'A 在跑', 5000)
_mk_sess_task('wd_projB_bbb222', 'sess_B', 'bash-b1', 'running', 'B 在跑', 4000)

_pay = statusline.tasks_payload()
check('看板服务:全会话分组(2 个会话)', len(_pay['sessions']) == 2)
check('看板服务:每个会话内 running 排最前',
      all(s['tasks'] and s['tasks'][0]['status'] == 'running' for s in _pay['sessions']))
check('看板服务:会话标签去掉 wd_ 前缀和 hash 后缀',
      any(s['label'] == 'projA' for s in _pay['sessions']))

# HTTP 端点(线程内起临时端口,port 0 = 系统分配)
import threading  # noqa: E402
import urllib.request  # noqa: E402
_srv, _port = statusline.start_tasks_server(port=0)
try:
    _html = urllib.request.urlopen(f'http://127.0.0.1:{_port}/', timeout=5).read().decode()
    _data = json.loads(urllib.request.urlopen(f'http://127.0.0.1:{_port}/data', timeout=5).read().decode())
finally:
    statusline.stop_tasks_server(_srv)
check('看板服务:/ 返回带局部刷新 JS 的页面', 'fetch(' in _html and '/data' in _html)
check('看板服务:/data 返回全会话任务', len(_data.get('sessions', [])) == 2)

# 段链接:服务在线走 http,离线回退 file:// 看板
statusline.board_url = lambda: 'http://127.0.0.1:18989/'
_seg = statusline.task_segment([{'taskId': 'x', 'status': 'running', 'startedAt': 1}])
check('看板服务:段链接走 http 服务', ']8;;http://127.0.0.1:18989/' in _seg)
statusline.board_url = lambda: None
_seg2 = statusline.task_segment([{'taskId': 'x', 'status': 'running', 'startedAt': 1}])
check('看板服务:服务离线回退 file:// 看板', ']8;;file://' in _seg2)

# 真实数据场景:死会话的幽灵 running 不能当真;2h 前的旧完成任务不进看板
_mk_sess_task('wd_projC_ccc333', 'sess_C', 'bash-ghost', 'running', '幽灵进程', 1000)
_g = os.path.join(tmp_srv, 'wd_projC_ccc333', 'sess_C', 'agents', 'main', 'tasks', 'bash-ghost.json')
_t = json.load(open(_g))
_t['pid'] = 99999999  # 必死的 pid
json.dump(_t, open(_g, 'w'))
_mk_sess_task('wd_projC_ccc333', 'sess_C', 'bash-live', 'running', '活进程', 1000)
_l = os.path.join(tmp_srv, 'wd_projC_ccc333', 'sess_C', 'agents', 'main', 'tasks', 'bash-live.json')
_t = json.load(open(_l))
_t['pid'] = os.getpid()  # 活着的 pid
json.dump(_t, open(_l, 'w'))
_mk_sess_task('wd_projC_ccc333', 'sess_C', 'bash-old', 'completed', '三小时前',
              1000, int(now * 1000) - 3 * 3600 * 1000)
_pay2 = statusline.tasks_payload()
_c = [s for s in _pay2['sessions'] if s['sid'] == 'sess_C'][0]['tasks']
_byid = {t['taskId']: t['status'] for t in _c}
check('看板服务:死 pid 的 running 降级 lost', _byid.get('bash-ghost') == 'lost')
check('看板服务:活 pid 保持 running', _byid.get('bash-live') == 'running')
check('看板服务:2h 前的已完成任务不进看板', 'bash-old' not in _byid)

# http→file 被浏览器禁跳:日志改走同源 /log 路由;agent 任务链 wire.jsonl 转录
_ad = os.path.join(tmp_srv, 'wd_projD_ddd444', 'sess_D', 'agents', 'main', 'tasks')
os.makedirs(_ad)
json.dump({'taskId': 'agent-x1', 'status': 'running', 'description': '子代理在跑',
           'kind': 'agent', 'agentId': 'agent-7', 'startedAt': 6000, 'detached': True},
          open(os.path.join(_ad, 'agent-x1.json'), 'w'))
_aw = os.path.join(tmp_srv, 'wd_projD_ddd444', 'sess_D', 'agents', 'agent-7')
os.makedirs(_aw)
open(os.path.join(_aw, 'wire.jsonl'), 'w').write('{"type":"x"}\n')
# bash-a2 的 output.log 要在构建 payload 前就存在(_log_url 只在文件存在时给出)
_lp = os.path.join(tmp_srv, 'wd_projA_aaa111', 'sess_A', 'agents', 'main', 'tasks', 'bash-a2', 'output.log')
os.makedirs(os.path.dirname(_lp), exist_ok=True)
open(_lp, 'w').write('hello log')
_pay3 = statusline.tasks_payload()
_ax = [t for s in _pay3['sessions'] for t in s['tasks'] if t['taskId'] == 'agent-x1']
check('看板服务:agent 任务给 wire 转录链接',
      bool(_ax) and '/log?p=' in _ax[0].get('_log_url', '') and 'wire.jsonl' in _ax[0]['_log_url'])
_pa = [t for s in _pay3['sessions'] for t in s['tasks'] if t['taskId'] == 'bash-a2']
check('看板服务:process 任务给 output 链接', bool(_pa) and '/log?p=' in _pa[0].get('_log_url', ''))

import urllib.parse  # noqa: E402
import urllib.error  # noqa: E402
_srv2, _port2 = statusline.start_tasks_server(port=0)
try:
    _lp = os.path.join(tmp_srv, 'wd_projA_aaa111', 'sess_A', 'agents', 'main', 'tasks', 'bash-a2', 'output.log')
    os.makedirs(os.path.dirname(_lp), exist_ok=True)
    open(_lp, 'w').write('hello log')
    _got = urllib.request.urlopen(
        f'http://127.0.0.1:{_port2}/log?p=' + urllib.parse.quote(_lp), timeout=5).read().decode()
    _code = None
    try:
        urllib.request.urlopen(
            f'http://127.0.0.1:{_port2}/log?p=' + urllib.parse.quote('/etc/hosts'), timeout=5)
    except urllib.error.HTTPError as e:
        _code = e.code
finally:
    statusline.stop_tasks_server(_srv2)
check('看板服务:/log 回传会话内日志内容', _got == 'hello log')
check('看板服务:/log 拒绝会话目录外路径', _code == 403)

# 运行中的静默任务(如 sleep)output.log 尚未产生:链接照给,/log 回占位提示而非 403
_srv3, _port3 = statusline.start_tasks_server(port=0)
try:
    _missing = os.path.join(tmp_srv, 'wd_projA_aaa111', 'sess_A', 'agents', 'main', 'tasks', 'bash-quiet', 'output.log')
    _quiet = urllib.request.urlopen(
        f'http://127.0.0.1:{_port3}/log?p=' + urllib.parse.quote(_missing), timeout=5).read().decode()
finally:
    statusline.stop_tasks_server(_srv3)
check('看板服务:日志未产生时回占位提示(非 403)', len(_quiet) > 0)

print()
if FAILED:
    print(f'{len(FAILED)} 个用例失败')
    sys.exit(1)
print('全部通过')
