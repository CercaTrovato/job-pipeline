from __future__ import annotations

import difflib
import importlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from jp import packets, prompts


@dataclass
class RunResult:
    ok: int = 0
    failed: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    usage_reported: int = 0


@dataclass
class ApiConfig:
    base_url: str
    model: str
    protocol: str = "responses"
    reasoning_effort: str = "low"
    concurrency: int = 2
    timeout: int = 180
    key: str = field(default="", repr=False)


_api_usage = threading.local()


def _resolve_key(cfg: Dict[str, Any], api_cfg: Dict[str, Any]) -> str:
    """Resolve credentials only from the explicit public config contract."""
    try:
        settings = importlib.import_module("jp.settings")
    except ImportError:
        settings = None
    if settings is not None and hasattr(settings, "resolve_credentials"):
        try:
            value = settings.resolve_credentials(cfg)
            if value:
                return str(value)
        except ValueError:
            # Also accept the already-normalized llm.api credential fields.
            if not (api_cfg.get("credential") or api_cfg.get("credential_kind")):
                raise RuntimeError("API 后端缺少有效凭据配置") from None

    credential = api_cfg.get("credential") or {}
    llm = (cfg.get("llm") or {})
    kind = credential.get("kind") or api_cfg.get("credential_kind") or "env"
    name = credential.get("name") or api_cfg.get("credential_name") or api_cfg.get("key_env") or "JP_LLM_API_KEY"
    if kind == "env":
        return os.environ.get(name, "")
    if kind == "session":
        return str(api_cfg.get("_session_key") or "")
    if kind == "keyring":
        try:
            import keyring  # optional
        except ImportError as exc:
            raise RuntimeError("缺少 keyring 依赖") from exc
        # Credential target is an explicit keyring service/account pair.
        service, sep, account = str(name).partition(":")
        if not sep or not service or not account:
            raise RuntimeError("keyring 凭据 name 必须为 service:account")
        return keyring.get_password(service, account) or ""
    raise RuntimeError("不支持的凭据类型")


def load_api_config(cfg: Dict[str, Any]) -> ApiConfig:
    """Build API settings from explicit config; never inspect other tools' files."""
    try:
        settings = importlib.import_module("jp.settings")
    except ImportError:
        settings = None
    # Public callers supply the runtime config returned by Workspace.runtime_config().
    # Do not reload user config here: doing so would lose the in-memory session key.
    runtime = cfg
    llm = runtime.get("llm") or {}
    a = llm.get("api") or llm
    base, model = a.get("base_url"), (a.get("model") or llm.get("model"))
    protocol = a.get("protocol")
    if not base:
        raise RuntimeError("API 后端缺少显式 base_url 配置")
    if not model:
        raise RuntimeError("API 后端缺少显式 model 配置")
    if protocol not in ("responses", "chat_completions", "anthropic"):
        raise RuntimeError("API 后端必须显式配置 protocol: responses、chat_completions 或 anthropic")
    key = _resolve_key(runtime, a)
    if not key:
        raise RuntimeError("API 后端缺少凭据；请按 credential 配置设置环境变量、keyring 或当前会话密钥")
    return ApiConfig(base_url=str(base).rstrip("/"), model=str(model), protocol=protocol,
                     reasoning_effort=a.get("reasoning_effort", llm.get("reasoning_effort", "low")),
                     concurrency=int(a.get("concurrency", llm.get("concurrency", 2))),
                     timeout=int(a.get("timeout_sec", llm.get("timeout_sec", 180))), key=key)


def _usage_dict(usage: Any, protocol: str) -> Dict[str, Any]:
    def get(obj, *names):
        for name in names:
            val = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
            if val is not None:
                return val
        return None
    if usage is None:
        return {"input_tokens": None, "cached_input_tokens": None, "output_tokens": None}
    details = get(usage, "input_tokens_details", "prompt_tokens_details")
    details = details if details is not None else {}
    cached = get(details, "cached_tokens", "cache_read_input_tokens")
    if cached is None:
        cached = get(usage, "cache_read_input_tokens")
    input_tokens = get(usage, "input_tokens", "prompt_tokens")
    output_tokens = get(usage, "output_tokens", "completion_tokens")
    return {
        "input_tokens": int(input_tokens) if input_tokens is not None else None,
        "cached_input_tokens": int(cached) if cached is not None else None,
        "output_tokens": int(output_tokens) if output_tokens is not None else None,
    }


def _sdk_error(exc: Exception, api: ApiConfig) -> RuntimeError:
    status = getattr(exc, "status_code", None)
    msg = str(exc)
    if api.key:
        msg = msg.replace(api.key, "***")
    msg = re.sub(r"\b(?:sk|key|token)-[A-Za-z0-9_-]{8,}\b", "***", msg)
    # SDK exceptions may echo arbitrary request data; keep only a small sanitized summary.
    msg = " ".join(msg.split())[:240]
    err = RuntimeError("API 请求失败%s: %s" % ((" HTTP %s" % status) if status else "", msg or type(exc).__name__))
    err.status_code = status
    err.no_retry = status is not None and 400 <= status < 500
    return err


def _extract_text(response: Any, protocol: str) -> str:
    if protocol == "responses":
        value = getattr(response, "output_text", None)
        if value:
            return value
        data = response.model_dump() if hasattr(response, "model_dump") else response
        for item in data.get("output", []):
            for content in item.get("content", []) or []:
                if content.get("type") in ("output_text", "text"):
                    return content.get("text", "")
    elif protocol == "chat_completions":
        choice = response.choices[0]
        value = choice.message.content
        if value:
            return value
    else:
        for block in response.content:
            if getattr(block, "type", None) == "text":
                return block.text
    raise RuntimeError("接口未返回 JSON 文本")


def _schema_example(schema: Dict[str, Any], root: Optional[Dict[str, Any]] = None,
                    seen: Optional[set[str]] = None) -> Any:
    """Create a compact illustrative value; full conformance remains local validation."""
    root = root or schema
    seen = seen or set()
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        if ref in seen:
            return {}
        target: Any = root
        for part in ref[2:].split("/"):
            target = target.get(part.replace("~1", "/").replace("~0", "~"), {}) if isinstance(target, dict) else {}
        return _schema_example(target if isinstance(target, dict) else {}, root, seen | {ref})
    if "const" in schema:
        return schema["const"]
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0]
    for branch in ("oneOf", "anyOf", "allOf"):
        options = schema.get(branch)
        if isinstance(options, list) and options:
            return _schema_example(options[0], root, seen)
    typ = schema.get("type")
    if isinstance(typ, list):
        typ = next((item for item in typ if item != "null"), typ[0] if typ else None)
    if typ == "object" or isinstance(schema.get("properties"), dict):
        return {key: _schema_example(value, root, seen) for key, value in schema.get("properties", {}).items()}
    if typ == "array":
        minimum = int(schema.get("minItems", 1))
        return [_schema_example(schema.get("items", {}), root, seen) for _ in range(min(minimum, 2))]
    if typ == "string":
        return "example"
    if typ == "integer":
        return 0
    if typ == "number":
        return 0.0
    if typ == "boolean":
        return False
    if typ == "null":
        return None
    return {}


def call_api(prompt: str, schema: Dict[str, Any], api: ApiConfig, schema_name: str) -> Dict[str, Any]:
    """Use the selected official SDK and protocol, then parse JSON locally."""
    _api_usage.value = {"input_tokens": None, "cached_input_tokens": None,
                        "output_tokens": None, "reported": False}
    try:
        if api.protocol in ("responses", "chat_completions"):
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError("缺少 openai SDK 依赖") from exc
            client = OpenAI(api_key=api.key, base_url=api.base_url, timeout=api.timeout, max_retries=0)
            if api.protocol == "responses":
                response = client.responses.create(
                    model=api.model, input=[{"role": "user", "content": prompt}],
                    reasoning={"effort": api.reasoning_effort},
                    text={"format": {"type": "json_schema", "name": schema_name,
                                     "schema": schema, "strict": False}},
                )
            else:
                example = json.dumps(_schema_example(schema), ensure_ascii=False, separators=(",", ":"))
                schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
                response = client.chat.completions.create(
                    model=api.model,
                    messages=[
                        {"role": "system", "content": (
                            "Output valid json only. Return one JSON object matching this schema exactly. "
                            "Do not add markdown or explanatory text. JSON example: %s\nJSON schema: %s"
                            % (example, schema_text))},
                        {"role": "user", "content": prompt},
                    ],
                    response_format={"type": "json_object"},
                )
            _api_usage.value = _usage_dict(getattr(response, "usage", None), api.protocol)
            _api_usage.value["reported"] = getattr(response, "usage", None) is not None
        elif api.protocol == "anthropic":
            try:
                from anthropic import Anthropic
            except ImportError as exc:
                raise RuntimeError("缺少 anthropic SDK 依赖") from exc
            client = Anthropic(api_key=api.key, base_url=api.base_url, timeout=api.timeout, max_retries=0)
            response = client.messages.create(
                model=api.model, max_tokens=4096,
                # Stable system prefix permits Anthropic prompt caching; schema stays native.
                system=[{"type": "text", "text": "请严格依据用户提供的任务包内容作答，并遵守输出 JSON schema。",
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": prompt}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
            usage = getattr(response, "usage", None)
            _api_usage.value = _usage_dict(usage, api.protocol)
            _api_usage.value["reported"] = usage is not None
        else:
            raise RuntimeError("未知 API protocol")
        text = _extract_text(response, api.protocol)
        try:
            out = json.loads(text)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("接口返回内容不是有效 JSON") from exc
        return out
    except RuntimeError:
        raise
    except Exception as exc:
        raise _sdk_error(exc, api) from None


def _scrub(msg: str, key: str) -> str:
    if key:
        msg = msg.replace(key, "***")
    return re.sub(r"\b(?:sk|key|token)-[A-Za-z0-9_-]{8,}\b", "***", msg)


def _fill_one(p: packets.Packet, api: ApiConfig, retries: int = 1) -> bool:
    last = ""
    usage = {"protocol": api.protocol, "model": api.model, "attempts": 0,
             "input_tokens": None, "cached_input_tokens": None, "output_tokens": None, "reported_attempts": 0}
    try:
        prompt = (p.dir / "prompt.md").read_text(encoding="utf-8")
        schema = json.loads((p.dir / "schema.json").read_text(encoding="utf-8"))
        name = "%s_%s" % (p.step, schema.get("$id", "v").replace("/", "_").replace(":", "_"))
        for attempt in range(retries + 1):
            usage["attempts"] += 1
            try:
                out = None
                try:
                    out = call_api(prompt, schema, api, name)
                finally:
                    attempt_usage = getattr(_api_usage, "value", {})
                    for k in ("input_tokens", "cached_input_tokens", "output_tokens"):
                        value = attempt_usage.get(k)
                        if value is not None:
                            usage[k] = (usage[k] or 0) + int(value)
                    usage["reported_attempts"] += int(attempt_usage.get("reported", False))
                assert out is not None
                (p.dir / "output.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
                packets.validate(p)
                _ground_analyze_quotes(p, out)
                packets.validate(p)
                (p.dir / "usage.json").write_text(json.dumps(usage, ensure_ascii=False, indent=1), encoding="utf-8")
                return True
            except Exception as exc:  # sanitized error artifact only
                last = _scrub("%s: %s" % (type(exc).__name__, exc), api.key)[:500]
                if getattr(exc, "no_retry", False) or attempt >= retries:
                    break
    except Exception as exc:
        last = _scrub("%s: %s" % (type(exc).__name__, exc), api.key)[:500]
    (p.dir / "error.txt").write_text("api: " + last, encoding="utf-8")
    output_path = p.dir / "output.json"
    if output_path.exists():
        output_path.unlink()
    (p.dir / "usage.json").write_text(json.dumps(usage, ensure_ascii=False, indent=1), encoding="utf-8")
    return False


def run_api(pks: List[packets.Packet], api: ApiConfig, on_done=None) -> RunResult:
    res = RunResult()
    with ThreadPoolExecutor(max_workers=max(1, api.concurrency)) as ex:
        for p, ok in zip(pks, ex.map(lambda packet: _fill_one(packet, api, retries=1), pks)):
            res.ok += int(ok)
            res.failed += int(not ok)
            if on_done:
                on_done()
            usage_path = p.dir / "usage.json"
            if usage_path.exists():
                usage = json.loads(usage_path.read_text(encoding="utf-8"))
                res.input_tokens += usage["input_tokens"] or 0
                res.cached_input_tokens += usage["cached_input_tokens"] or 0
                res.output_tokens += usage["output_tokens"] or 0
                res.usage_reported += int(usage["reported_attempts"] > 0)
    return res


def codex_executable() -> str:
    override = os.environ.get("JP_CODEX_BIN")
    if override:
        return override
    local = os.environ.get("LOCALAPPDATA")
    if local:
        binaries = list((pathlib.Path(local) / "OpenAI" / "Codex" / "bin").glob("*/codex.exe"))
        if binaries:
            return str(max(binaries, key=lambda p: p.stat().st_mtime))
    return shutil.which("codex") or "codex"


def codex_command(p: packets.Packet, reasoning_effort: str = "low", model: Optional[str] = None) -> List[str]:
    cmd = [codex_executable(), "exec", "--ignore-user-config"]
    if model:
        cmd += ["-m", model]
    cmd += ["-C", str(p.dir), "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
            "--color", "never", "--json", "-c", "model_reasoning_effort=%s" % reasoning_effort,
            "--output-schema", str(p.dir / "schema.json"), "-o", str(p.dir / "output.json"), "-"]
    return cmd


def _codex_usage(stdout: bytes) -> Optional[Dict[str, int]]:
    for line in reversed((stdout or b"").splitlines()):
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
            return {k: int(usage.get(k, 0)) for k in ("input_tokens", "cached_input_tokens", "output_tokens")}
    return None


def _codex_event_counts(stdout: bytes) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for line in (stdout or b"").splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        kind = event.get("type")
        if isinstance(kind, str):
            counts[kind] = counts.get(kind, 0) + 1
    return counts


def _ground_analyze_quotes(p: packets.Packet, out: Dict[str, Any]) -> None:
    if p.step != "analyze":
        return
    jd = json.loads((p.dir / "input.json").read_text(encoding="utf-8"))["jd_text"]
    repairs = []
    for req in out["atomic_requirements"]:
        quote = req["quote"]
        if quote in jd:
            continue
        hit = difflib.SequenceMatcher(None, quote, jd, autojunk=False).find_longest_match(0, len(quote), 0, len(jd))
        if hit.size < max(4, min(12, len(quote) // 2)):
            raise packets.PacketError("%s 引文无法定位到 JD 原文" % req["id"])
        grounded = jd[hit.b:hit.b + hit.size]
        repairs.append({"id": req["id"], "original": quote, "grounded": grounded})
        req["quote"] = grounded
    if len(repairs) > max(2, len(out["atomic_requirements"]) // 10):
        raise packets.PacketError("引文未逐字引用原文的比例过高: %d 条" % len(repairs))
    if repairs:
        (p.dir / "output.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        (p.dir / "quote_repairs.json").write_text(json.dumps(repairs, ensure_ascii=False, indent=1), encoding="utf-8")


def _fill_codex_one(p: packets.Packet, timeout: int, retries: int, reasoning_effort: str, model: Optional[str]) -> bool:
    out_path = p.dir / "output.json"
    last = "未执行"
    usage = {"model": model, "reasoning_effort": reasoning_effort, "attempts": 0,
             "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reported_attempts": 0,
             "event_counts": {}}
    start = time.monotonic()
    for _ in range(retries + 1):
        usage["attempts"] += 1
        try:
            prompt = prompts.for_codex((p.dir / "prompt.md").read_text(encoding="utf-8")).encode("utf-8")
            cp = subprocess.run(codex_command(p, reasoning_effort, model), input=prompt,
                                timeout=timeout, capture_output=True)
            for kind, count in _codex_event_counts(getattr(cp, "stdout", b"")).items():
                usage["event_counts"][kind] = usage["event_counts"].get(kind, 0) + count
            reported = _codex_usage(getattr(cp, "stdout", b""))
            if reported:
                usage["reported_attempts"] += 1
                for k, v in reported.items():
                    usage[k] += v
            if cp.returncode:
                raise RuntimeError("CLI 退出 %d" % cp.returncode)
            out = packets.validate(p)
            _ground_analyze_quotes(p, out)
            packets.validate(p)
            break
        except (OSError, subprocess.TimeoutExpired, RuntimeError, packets.PacketError) as e:
            last = "%s: %s" % (type(e).__name__, e)
            if out_path.exists():
                out_path.unlink()
    else:
        (p.dir / "error.txt").write_text("codex: " + last[-500:], encoding="utf-8")
    usage["elapsed_sec"] = round(time.monotonic() - start, 1)
    (p.dir / "usage.json").write_text(json.dumps(usage, ensure_ascii=False, indent=1), encoding="utf-8")
    return out_path.exists()


def run_codex(pks: List[packets.Packet], timeout: int = 180, retries: int = 1, on_done=None,
              reasoning_effort: str = "low", model: Optional[str] = None, concurrency: int = 2) -> RunResult:
    res = RunResult()
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futures = [ex.submit(_fill_codex_one, p, timeout, retries, reasoning_effort, model) for p in pks]
        for future in as_completed(futures):
            ok = future.result()
            res.ok += int(ok)
            res.failed += int(not ok)
            if on_done:
                on_done()
    for p in pks:
        path = p.dir / "usage.json"
        if path.exists():
            usage = json.loads(path.read_text(encoding="utf-8"))
            res.input_tokens += usage["input_tokens"]
            res.cached_input_tokens += usage["cached_input_tokens"]
            res.output_tokens += usage["output_tokens"]
            res.usage_reported += int(usage["reported_attempts"] > 0)
    return res
