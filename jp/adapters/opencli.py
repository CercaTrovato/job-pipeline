from __future__ import annotations
import json
import shutil
import subprocess
import contextvars
from contextlib import contextmanager
from typing import Any, Callable, List, Optional, Tuple

from jp.adapters.base import AdapterError, AuthRequiredError, RiskControlError, looks_like_risk

OPENCLI = shutil.which("opencli") or "opencli"      # Windows 下是 npm 的 .cmd 垫片，必须用完整路径
_NOISE = ("UNDICI", "trace-warnings", "Update available", "npm install -g")
_PROFILE = contextvars.ContextVar("opencli_profile", default="")


@contextmanager
def using_profile(profile):
    token = _PROFILE.set(profile or "")
    try:
        yield
    finally:
        _PROFILE.reset(token)


def _clean(text: str) -> str:
    """去掉 node 警告与升级提示行。"""
    return "\n".join(ln for ln in (text or "").splitlines() if not any(n in ln for n in _NOISE)).strip()


def _first_line(text: str) -> str:
    for ln in text.splitlines():
        if ln.strip():
            return ln.strip()[:160]
    return ""


def _extract_json(text: str) -> Any:
    """从第一个 '[' / '{' 开始解析（opencli 偶尔在前面打一行提示）。"""
    starts = [i for i in (text.find("["), text.find("{")) if i >= 0]
    if not starts:
        raise ValueError("no json")
    return json.loads(text[min(starts):])


def _run(cmd: List[str], timeout: int, runner: Callable[..., subprocess.CompletedProcess]) -> subprocess.CompletedProcess:
    if _PROFILE.get():
        cmd = [cmd[0], "--profile", _PROFILE.get()] + cmd[1:]
    return runner(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def run_opencli(args: List[str], timeout: int = 180, retries: int = 1,
                runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> Any:
    """执行 `opencli <args>`（调用方自带 -f json）并返回解析后的 JSON。
    - exit 77 或输出含 AUTH_REQUIRED → AuthRequiredError（提示用户去 Edge 登录）
    - 风控字样只在错误信息 / 非 JSON 输出里判定，绝不扫描成功返回的数据 → RiskControlError（哨兵冷却）
    - 空输出（冷启动偶发）重试 retries 次，仍空 → AdapterError
    - ok:false 其它 code → AdapterError
    """
    label = " ".join(args[:2])
    last = ""
    for _ in range(retries + 1):
        try:
            cp = _run([OPENCLI] + list(args), timeout, runner)
        except (OSError, subprocess.SubprocessError) as e:
            raise AdapterError("opencli %s: 子进程失败: %s" % (label, e))
        out, err = _clean(cp.stdout), _clean(cp.stderr)
        combined = out + "\n" + err
        if cp.returncode == 77 or "AUTH_REQUIRED" in combined:
            raise AuthRequiredError("opencli %s: 未登录（%s）——请在 Edge 里登录后重跑" % (label, _first_line(combined)))
        if out:
            try:
                data = _extract_json(out)
            except ValueError:
                if looks_like_risk(out):
                    raise RiskControlError("opencli %s: 疑似风控（%s）" % (label, _first_line(out)))
                raise AdapterError("opencli %s: 输出不是 JSON: %s" % (label, out[:200]))
            if isinstance(data, dict) and data.get("ok") is False:
                e = data.get("error") or {}
                if e.get("code") == "AUTH_REQUIRED":
                    raise AuthRequiredError("opencli %s: %s" % (label, e.get("message", "")))
                if looks_like_risk(e.get("message", "")):
                    raise RiskControlError("opencli %s: 疑似风控（%s）" % (label, e.get("message", "")))
                raise AdapterError("opencli %s: %s %s" % (label, e.get("code", ""), e.get("message", "")))
            return data
        if looks_like_risk(err):
            raise RiskControlError("opencli %s: 疑似风控（%s）" % (label, _first_line(err)))
        last = err or ("exit %s" % cp.returncode)
    raise AdapterError("opencli %s: 无输出（%s）" % (label, last[:200]))


def daemon_restart(runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> bool:
    """`opencli daemon restart`：换一轮浏览器会话租约。会话卡在 about:blank（租约失效）时的唯一有效恢复手段，
    真机验证过 close + 重开无效。输出不是 JSON，这里只看退出码，失败不抛（调用方随后会自己再确认页面）。"""
    try:
        cp = _run([OPENCLI, "daemon", "restart"], 120, runner)
    except (OSError, subprocess.SubprocessError):
        return False
    return cp.returncode == 0


def doctor_ok(runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> Tuple[bool, str]:
    """`opencli doctor`：扩展是否连上（Edge 开着且 OpenCLI 扩展启用）。"""
    try:
        cp = _run([OPENCLI, "doctor"], 60, runner)
    except (OSError, subprocess.SubprocessError) as e:
        return False, "opencli 不可用: %s" % e
    text = _clean((cp.stdout or "") + "\n" + (cp.stderr or ""))
    return ("Extension: connected" in text and "Connectivity: connected" in text), text


def browser_eval(session: str, url: Optional[str], js: str, timeout: int = 120,
                 runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str:
    """在 OpenCLI 浏览器会话里执行 JS（可选先打开 url，后台窗口），返回清洗后的 stdout。
    注意：js 里不要出现 '%'（Windows 的 .cmd 垫片会经 cmd.exe，'%x%' 被当变量展开）。
    风控字样只在非 JSON 输出上判定；输出是 JSON 时原样返回，绝不扫描其内容——除非那段 JSON 本身
    是 OpenCLI 自己的基础设施错误包装（形如 {"error": {"code": ..., "message": ...}}，顶层只有
    "error" 一个键），例如 CDP 命令超时；那种情况下没有任何"站点业务数据"可言，直接转成 AdapterError，
    不能原样交给调用方当成站点响应解析（会把顶层缺的 code 读成 None，掩盖真正的基础设施错误）。"""
    if url:
        try:
            cp = _run([OPENCLI, "browser", session, "open", url, "--window", "background"], timeout, runner)
        except (OSError, subprocess.SubprocessError) as e:
            raise AdapterError("opencli browser %s: 子进程失败: %s" % (session, e))
        if cp.returncode != 0 and not _clean(cp.stdout):
            raise AdapterError("opencli browser open 失败: %s" % _first_line(_clean(cp.stderr)))
    try:
        cp = _run([OPENCLI, "browser", session, "eval", js], timeout, runner)
    except (OSError, subprocess.SubprocessError) as e:
        raise AdapterError("opencli browser %s: 子进程失败: %s" % (session, e))
    out = _clean(cp.stdout)
    if not out:
        raise AdapterError("opencli browser eval 无输出: %s" % _first_line(_clean(cp.stderr)))
    try:
        data = json.loads(out)
    except ValueError:
        if looks_like_risk(out):
            raise RiskControlError("browser eval 页面疑似风控: %s" % _first_line(out))
        return out
    if isinstance(data, dict) and set(data.keys()) == {"error"} and isinstance(data["error"], dict) \
            and "code" in data["error"]:
        err = data["error"]
        raise AdapterError("opencli browser eval 失败: %s %s" % (err.get("code"), err.get("message", "")))
    return out


def browser_close(session: str, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
    try:
        _run([OPENCLI, "browser", session, "close"], 30, runner)
    except (OSError, subprocess.SubprocessError):
        pass
