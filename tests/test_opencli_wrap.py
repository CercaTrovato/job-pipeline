from __future__ import annotations
import subprocess
import pytest
from jp.adapters import opencli as oc
from jp.adapters.base import AdapterError, AuthRequiredError, RiskControlError

NOISE = "(node:1) [UNDICI-EHPA] Warning: EnvHttpProxyAgent is experimental\n(Use `node --trace-warnings ...`)\n\n  Update available: v1.8.6 → v1.8.7\n  Run: npm install -g @jackwener/opencli\n"


def _cp(stdout="", stderr=NOISE, code=0):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr=stderr)


def test_parses_json_from_stdout_and_passes_args():
    calls = []
    def runner(cmd, **kw):
        calls.append(cmd)
        return _cp('[{"name": "豆包大模型评测实习生"}]')
    data = oc.run_opencli(["boss", "search", "大模型 实习", "-f", "json"], runner=runner)
    assert data == [{"name": "豆包大模型评测实习生"}]
    assert calls[0][0] == oc.OPENCLI and calls[0][1:] == ["boss", "search", "大模型 实习", "-f", "json"]


def test_exit_77_and_ok_false_auth_required():
    with pytest.raises(AuthRequiredError):
        oc.run_opencli(["boss", "whoami", "-f", "json"], runner=lambda *a, **k: _cp("", code=77))
    yaml_like = 'ok: false\nerror:\n  code: AUTH_REQUIRED\n  message: Boss wt2 / t cookies missing\n'
    with pytest.raises(AuthRequiredError):
        oc.run_opencli(["boss", "whoami", "-f", "json"], runner=lambda *a, **k: _cp(yaml_like, code=1))
    js = '{"ok": false, "error": {"code": "AUTH_REQUIRED", "message": "Nowcoder t cookie missing"}}'
    with pytest.raises(AuthRequiredError):
        oc.run_opencli(["nowcoder", "whoami", "-f", "json"], runner=lambda *a, **k: _cp(js))


def test_ok_false_message_with_risk_text_is_risk():
    with pytest.raises(RiskControlError):
        oc.run_opencli(["boss", "search", "x", "-f", "json"], runner=lambda *a, **k: _cp('{"ok": false, "error": {"code": "UNKNOWN", "message": "unusual activity detected"}}'))


def test_non_json_output_with_risk_text_is_risk():
    with pytest.raises(RiskControlError):
        oc.run_opencli(["boss", "search", "x", "-f", "json"], runner=lambda *a, **k: _cp("请完成安全验证后继续", code=1))


def test_successful_json_payload_is_never_risk_checked():
    payload = '[{"title": "SRE Intern", "description": "Design API rate limit and captcha flows"}]'
    data = oc.run_opencli(["linkedin", "job-detail", "x", "-f", "json"], runner=lambda *a, **k: _cp(payload))
    assert data == [{"title": "SRE Intern", "description": "Design API rate limit and captcha flows"}]


def test_empty_output_retries_once_then_adapter_error():
    n = {"calls": 0}
    def runner(cmd, **kw):
        n["calls"] += 1
        return _cp("", code=1) if n["calls"] == 1 else _cp('{"ok": true}')
    assert oc.run_opencli(["boss", "search", "x", "-f", "json"], runner=runner) == {"ok": True}
    assert n["calls"] == 2
    with pytest.raises(AdapterError):
        oc.run_opencli(["boss", "search", "x", "-f", "json"], runner=lambda *a, **k: _cp("", code=1))


def test_ok_false_other_code_is_adapter_error():
    with pytest.raises(AdapterError):
        oc.run_opencli(["linkedin", "search", "x", "-f", "json"], runner=lambda *a, **k: _cp('{"ok": false, "error": {"code": "UNKNOWN", "message": "Text not found: Jobs"}}'))


def test_doctor_ok_parses_connected_lines():
    good = "opencli v1.8.6 doctor\n[OK] Daemon: running on port 19825\n[OK] Extension: connected (v1.0.24)\n[OK] Connectivity: connected in 0.2s\n"
    bad = "[OK] Daemon: running\n[MISSING] Extension: not connected\n[FAIL] Connectivity: failed\n"
    assert oc.doctor_ok(runner=lambda *a, **k: _cp(good))[0] is True
    ok, text = oc.doctor_ok(runner=lambda *a, **k: _cp(bad))
    assert ok is False and "not connected" in text


def test_browser_eval_opens_then_evals_and_returns_clean_text():
    calls = []
    def runner(cmd, **kw):
        calls.append(cmd[1:])
        return _cp('{"url": "https://www.nowcoder.com/"}\n' if cmd[3] == "open" else '{"code":0}\n')
    out = oc.browser_eval("nk", "https://www.nowcoder.com/", "1+1", runner=runner)
    assert out == '{"code":0}'
    assert calls[0] == ["browser", "nk", "open", "https://www.nowcoder.com/", "--window", "background"]
    assert calls[1] == ["browser", "nk", "eval", "1+1"]
    out2 = oc.browser_eval("nk", None, "2", runner=lambda cmd, **kw: _cp("2\n"))
    assert out2 == "2"


def test_browser_eval_json_output_not_risk_checked():
    payload = '{"code":0,"msg":"OK","data":{"datas":[{"data":{"jobName":"反爬 captcha 平台实习生"}}]}}'
    out = oc.browser_eval("nk", None, "1+1", runner=lambda cmd, **kw: _cp(payload))
    assert out == payload
    with pytest.raises(RiskControlError):
        oc.browser_eval("nk", None, "1+1", runner=lambda cmd, **kw: _cp("操作频繁，请稍后再试"))


def test_browser_eval_infra_error_envelope_raises_adapter_error():
    envelope = ('{"error": {"code": "cdp_timeout", "message": "CDP command Runtime.evaluate timed out '
                'after 115s — the page may be blocked by a native dialog (alert/confirm/print)"}}')
    with pytest.raises(AdapterError) as exc_info:
        oc.browser_eval("nk", None, "1+1", runner=lambda cmd, **kw: _cp(envelope))
    assert "cdp_timeout" in str(exc_info.value)
    # 站点自己的业务 JSON（哪怕顶层也叫 code/message）只要不是纯 {"error": {...}} 包装形状，仍原样返回，不被误判
    site_json = '{"code":37,"message":"x"}'
    out = oc.browser_eval("nk", None, "1+1", runner=lambda cmd, **kw: _cp(site_json))
    assert out == site_json


def test_run_opencli_subprocess_error_becomes_adapter_error():
    def runner(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd=["opencli"], timeout=1)
    with pytest.raises(AdapterError):
        oc.run_opencli(["boss", "search", "x", "-f", "json"], runner=runner)


def test_browser_eval_subprocess_error_becomes_adapter_error():
    def runner(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd=["opencli"], timeout=1)
    with pytest.raises(AdapterError):
        oc.browser_eval("nk", "https://www.nowcoder.com/", "1+1", runner=runner)
    with pytest.raises(AdapterError):
        oc.browser_eval("nk", None, "1+1", runner=runner)


def test_browser_close_calls_close_and_swallows_oserror():
    calls = []
    def runner(cmd, **kw):
        calls.append(cmd)
        raise OSError("boom")
    oc.browser_close("nk", runner=runner)
    assert calls[0] == [oc.OPENCLI, "browser", "nk", "close"]
