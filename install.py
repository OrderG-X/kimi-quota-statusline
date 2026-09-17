#!/usr/bin/env python3
"""kimi-quota-statusline 跨平台安装器:把 tui.toml 的 [status_line].command 指向本插件。

幂等;改动前自动备份(tui.toml.时间戳.bak);已有其他 command 会提示覆盖。
macOS / Linux 可用兼容壳 install.sh;Windows 直接 `python install.py`。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time

SECTION_RE = r'(?ms)^\[status_line\][ \t]*\r?\n.*?(?=^\[|\Z)'


def _write_atomic(path, text):
    """原子覆盖写:先写临时文件再替换,避免写一半崩溃留下截断的配置。"""
    tmp = path + '.tmp'
    open(tmp, 'w', encoding='utf-8').write(text)
    os.replace(tmp, path)


# cmd /c 下的元字符:含任一字符(或空格)就必须加引号;`,` `;` `=` 在 cmd 命令解析里
# 也当参数分隔符(C:\a,b\x 会被切碎),一并列入
_CMD_UNSAFE = set(' &|<>^()%"!,;=')


def _cmd_needs_quotes(s):
    return any(ch in _CMD_UNSAFE for ch in s)


def build_command(target, os_name=None, executable=None):
    """生成 tui.toml 里的 command 行。
    posix 沿用 `python3 <路径>`;Windows 用解释器绝对路径,反斜杠按 TOML 规则双写转义。
    Windows 路径不含空格/cmd 元字符时不加引号:TUI spawn cmd.exe 的引号形态各版本不一
    (libuv 默认 quoting 会把内嵌引号转成 \\" 喂给 cmd,cmd 不认反斜杠转义,命令直接
    失败回退内置布局),裸命令在所有已知 spawn 形态下都能跑;含元字符才退回引号形态。"""
    os_name = os_name or os.name
    if os_name == 'nt':
        raw_exe = executable or sys.executable
        exe = raw_exe.replace('\\', '\\\\')
        tgt = target.replace('\\', '\\\\')
        if not _cmd_needs_quotes(raw_exe) and not _cmd_needs_quotes(target):
            return f'command = "{exe} {tgt}"'
        print('提示:解释器或插件路径含空格/cmd 元字符,命令行退回引号形态——'
              '该形态在部分 CLI 构建的 TUI spawn 下可能静默失效(回退内置布局);'
              '若 /reload-tui 后状态栏未出现,请把插件移到无空格路径后重装')
        return f'command = "\\"{exe}\\" \\"{tgt}\\""'
    return f'command = "python3 {target}"'


def _doctor(tui):
    """kimi doctor tui 校验。Windows 下 kimi 常是 npm 生成的 kimi.cmd shim,
    CreateProcess 无法直接执行 .cmd(抛 WinError 193),须经 cmd /c(shell=True)转一道;
    运行层面失败只提示不阻断——配置已写入,是否生效以 /reload-tui 实况为准。"""
    if not shutil.which('kimi'):
        return 0
    try:
        r = subprocess.run(['kimi', 'doctor', 'tui', tui],
                           check=False, shell=(os.name == 'nt'))
    except OSError:
        print('提示:kimi doctor 未能自动运行,运行 /reload-tui 观察状态栏是否生效即可')
        return 0
    if r.returncode != 0:
        return r.returncode
    print('kimi doctor 校验通过')
    return 0


def install(tui, target, os_name=None, executable=None, doctor=True):
    """把 [status_line].command 写入 tui(指向 target)。返回退出码。"""
    cmd_line = build_command(target, os_name, executable)
    text = open(tui, encoding='utf-8').read() if os.path.exists(tui) else ''
    if text and not text.endswith('\n'):
        text += '\n'  # 裸 [status_line] 收尾的文件,补换行让 section 可被匹配

    m = re.search(SECTION_RE, text)
    sec = m.group(0) if m else ''
    if target in sec or target.replace('\\', '\\\\') in sec:
        print('已安装:[status_line].command 已指向本插件,无需变更')
        return 0

    if re.search(r'(?m)^command\s*=', sec):
        print('提示:[status_line].command 当前指向其他脚本,将被覆盖为插件版本(原文件已备份)')

    if os.path.exists(tui):
        shutil.copy(tui, f'{tui}.{time.strftime("%Y%m%d-%H%M%S")}.bak')

    if sec:
        def repl(m):
            body = m.group(0)
            if re.search(r'(?m)^command\s*=', body):
                # 替换串走 lambda:re.sub 会把 cmd_line 里 Windows 路径的 \\ 当转义吃掉
                return re.sub(r'(?m)^command\s*=.*$', lambda _: cmd_line, body, count=1)
            return body.rstrip('\n') + '\n' + cmd_line + '\n'
        text = re.sub(SECTION_RE, repl, text)
    else:
        text += f'\n[status_line]\n{cmd_line}\n'

    os.makedirs(os.path.dirname(tui), exist_ok=True)
    _write_atomic(tui, text)
    print(f'已写入: {tui}')

    if doctor:
        return _doctor(tui)
    return 0


def patch_managed_hook(managed_file=None, os_name=None, executable=None):
    """Windows:把托管副本 manifest 里 hook 命令的 python3 换成解释器绝对路径。

    manifest 是静态 JSON 只能写死 `python3`(POSIX 必有);Windows 常只有 python.exe,
    安装时顺手改写托管副本。hook 命令以插件根目录为 cwd,`./statusline.py` 相对路径不变。
    失败(托管副本不存在/不可读)只跳过——不影响状态栏主功能。"""
    if (os_name or os.name) != 'nt':
        return
    mf = managed_file or os.path.join(
        os.environ.get('KIMI_CODE_HOME', os.path.expanduser('~/.kimi-code')),
        'plugins', 'managed', 'kimi-quota-statusline', 'kimi.plugin.json')
    try:
        data = json.load(open(mf, encoding='utf-8'))
        exe = executable or sys.executable
        changed = False
        for h in data.get('hooks') or []:
            cmd = h.get('command', '')
            if cmd.startswith('python3 '):
                h['command'] = f'"{exe}"' + cmd[len('python3'):]
                changed = True
        if changed:
            json.dump(data, open(mf, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
            print('提示:已把托管插件 hook 的解释器换成本机 Python 绝对路径(Windows)')
    except (OSError, ValueError):
        pass


def main():
    # Windows 控制台 stdout 默认 locale 编码(cp1252/GBK),中文提示会炸;强制 UTF-8
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    target = os.path.join(plugin_dir, 'statusline.py')
    if not os.path.isfile(target):
        print(f'错误: {target} 不存在')
        return 1
    kc_home = os.environ.get('KIMI_CODE_HOME', os.path.expanduser('~/.kimi-code'))
    rc = install(os.path.join(kc_home, 'tui.toml'), target)
    patch_managed_hook()  # Windows:托管副本 hook 命令的 python3 换解释器绝对路径(无副本则跳过)
    if rc == 0:
        print('安装完成。在 Kimi Code TUI 中运行 /reload-tui 立即生效(或重开新会话)。')
        print('觉得好用的话,欢迎给个 Star ⭐ https://github.com/OrderG-X/kimi-quota-statusline')
    return rc


if __name__ == '__main__':
    sys.exit(main())
