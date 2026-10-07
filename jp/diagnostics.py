from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
from typing import Any, Dict

from jp import backends, packets


def _llm(cfg: Dict[str, Any]) -> Dict[str, Any]:
    # Caller passes the runtime config; reload would discard a session-only key.
    return cfg.get("llm") or {}


def _codex_auth() -> tuple[bool, str]:
    try:
        result = subprocess.run([backends.codex_executable(), "login", "status"],
                                capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False, "无法检查 Codex 登录状态"
    # Never expose stdout/stderr, which may contain machine-specific details.
    return result.returncode == 0, "Codex 登录状态检查完成" if result.returncode == 0 else "Codex 尚未登录或登录状态不可用"


def _scratch_dir(cfg: Dict[str, Any]) -> pathlib.Path:
    override = os.environ.get("JP_SCRATCH_DIR")
    if override:
        root = pathlib.Path(override).expanduser()
    else:
        tasks_dir = (cfg.get("paths") or {}).get("tasks_dir")
        if tasks_dir:
            root = pathlib.Path(tasks_dir).expanduser().resolve().parent / "tmp-diagnostics"
        else:
            try:
                from platformdirs import user_cache_dir
            except ImportError as exc:
                raise RuntimeError("缺少 platformdirs 依赖，无法创建诊断临时目录") from exc
            root = pathlib.Path(user_cache_dir("job-pipeline", appauthor=False)) / "tmp-diagnostics"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _codex_model_test(cfg: Dict[str, Any], llm: Dict[str, Any]) -> Dict[str, Any]:
    model = llm.get("codex_model") or llm.get("model")
    if not model:
        return {"status": "needs_model", "message": "请先配置要诊断的 Codex 模型", "tested": False,
                "next_step": "在 llm.model 中填写希望使用的 Codex 模型后重试"}
    scratch = _scratch_dir(cfg)
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}},
              "required": ["ok"], "additionalProperties": False}
    with tempfile.TemporaryDirectory(prefix="diagnostics-", dir=str(scratch)) as temp:
        root = pathlib.Path(temp)
        schema_path = root / "schema.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        p = packets.create(root / "tasks", "synthetic-check", "profile", {"sample": True},
                           "Return JSON with ok set to true.", schema_path)
        try:
            result = backends.run_codex([p], retries=0, timeout=int(llm.get("timeout_sec", 60)),
                                        reasoning_effort=llm.get("reasoning_effort", "low"), model=model,
                                        concurrency=1)
            if result.ok == 1 and packets.validate(p).get("ok") is True:
                return {"status": "configured", "message": "Codex 模型调用与 JSON schema 校验成功",
                        "tested": True, "next_step": "可以使用该模型处理任务包"}
            return {"status": "connection_failed", "message": "Codex 模型调用或 JSON schema 校验失败",
                    "tested": True, "next_step": "检查 CLI 登录、模型可用性和模型输出能力"}
        except Exception:
            return {"status": "connection_failed", "message": "Codex 模型调用或 JSON schema 校验失败",
                    "tested": True, "next_step": "检查 CLI 登录、模型可用性和模型输出能力"}


def check(cfg: Dict[str, Any], test_model: bool = False) -> Dict[str, Any]:
    """Return a secret-free, Chinese setup diagnosis; checks are explicit and bounded."""
    llm = _llm(cfg)
    backend = llm.get("backend", "manual")
    checks = []
    tested = False
    if backend == "codex":
        logged_in, message = _codex_auth()
        checks.append({"name": "codex_login", "ok": logged_in, "message": message})
        if not logged_in:
            return {"status": "needs_login", "message": "Codex 尚未登录或无法确认登录状态", "checks": checks,
                    "tested": False, "next_step": "运行 codex login 完成订阅登录后重新诊断"}
        model = llm.get("codex_model") or llm.get("model")
        if not model:
            return {"status": "needs_model", "message": "Codex 尚未配置模型 ID", "checks": checks,
                    "tested": False, "next_step": "在 llm.model 中填写要使用的 Codex 模型 ID"}
        if test_model:
            result = _codex_model_test(cfg, llm)
            checks.append({"name": "codex_model", "ok": result["status"] == "configured",
                           "message": result["message"]})
            return {**result, "checks": checks}
        return {"status": "configured", "message": "Codex 已登录；模型调用未测试", "checks": checks,
                "tested": False, "next_step": "如需验证模型，请显式启用 test_model 并配置模型名"}

    if backend == "api":
        api_cfg = llm.get("api") or {}
        if not (api_cfg.get("model") or llm.get("model")):
            checks.append({"name": "api_model", "ok": False, "message": "尚未配置服务商模型 ID"})
            return {"status": "needs_model", "message": "尚未配置 API 模型 ID", "checks": checks,
                    "tested": False, "next_step": "在 llm.model 中填写服务商提供的模型 ID"}
        try:
            api = backends.load_api_config(cfg)
        except RuntimeError as exc:
            message = str(exc)
            if "model" in message.lower() or "模型" in message:
                status, next_step = "needs_model", "在 llm.api.model 中填写服务商提供的模型 ID"
            elif "凭据" in message or "key" in message.lower():
                status, next_step = "missing_key", "按 credential 配置设置环境变量、系统凭据库或当前会话密钥"
            else:
                status, next_step = "manual", "检查 llm.api 中显式配置的 protocol 和 base_url"
            checks.append({"name": "api_configuration", "ok": False, "message": message})
            return {"status": status, "message": message, "checks": checks, "tested": False, "next_step": next_step}
        checks.append({"name": "api_configuration", "ok": True, "message": "协议、地址、模型和凭据配置齐全"})
        if test_model:
            schema = {"type": "object", "properties": {"ok": {"type": "boolean"}},
                      "required": ["ok"], "additionalProperties": False}
            scratch = _scratch_dir(cfg)
            usage = {"protocol": api.protocol, "model": api.model, "attempts": 1,
                     "input_tokens": None, "cached_input_tokens": None, "output_tokens": None,
                     "reported_attempts": 0}
            with tempfile.TemporaryDirectory(prefix="api-check-", dir=str(scratch)) as temp:
                root = pathlib.Path(temp)
                schema_path = root / "schema.json"
                schema_path.write_text(json.dumps(schema), encoding="utf-8")
                packet = packets.create(root / "tasks", "synthetic-check", "profile", {"synthetic": True},
                                        "Return the JSON object {\"ok\": true}.", schema_path)
                try:
                    output = backends.call_api((packet.dir / "prompt.md").read_text(encoding="utf-8"),
                                               schema, api, "diagnostics_minimal")
                    (packet.dir / "output.json").write_text(json.dumps(output), encoding="utf-8")
                    validated = packets.validate(packet)
                    if validated.get("ok") is not True:
                        raise packets.PacketError("诊断输出未满足预期")
                    result = {"status": "configured", "message": "API 模型调用与 JSON schema 校验成功",
                              "tested": True, "next_step": "可以使用该模型处理任务包"}
                except Exception:
                    result = {"status": "connection_failed", "message": "API 模型调用或 JSON schema 校验失败",
                              "tested": True, "next_step": "检查 API 凭据、模型 ID、协议兼容性和 schema 支持"}
                finally:
                    reported = getattr(backends._api_usage, "value", {})
                    for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
                        usage[key] = reported.get(key)
                    usage["reported_attempts"] = int(reported.get("reported", False))
                    (packet.dir / "usage.json").write_text(json.dumps(usage, ensure_ascii=False, indent=1), encoding="utf-8")
            checks.append({"name": "api_model", "ok": result["status"] == "configured",
                           "message": result["message"]})
            return {**result, "checks": checks}
        return {"status": "configured", "message": "API 配置齐全；连接未测试", "checks": checks,
                "tested": False, "next_step": "如需验证 API 调用，请显式启用 test_model"}

    return {"status": "manual", "message": "当前后端由人工填写任务包，不需要自动登录检查",
            "checks": [{"name": "backend", "ok": True, "message": "人工任务包模式"}],
            "tested": False, "next_step": "填写任务包 output.json 后运行 resume"}
