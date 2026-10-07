"""用户工作区配置：只保存非敏感设置，凭据仅在运行时解析。"""
from __future__ import annotations

import copy
import ipaddress
import os
import pathlib
import re
import tempfile
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

import yaml
from platformdirs import user_data_dir


_RESOURCE = pathlib.Path(__file__).resolve().parent / "resources"
_SENSITIVE = re.compile(r"(?:api.?key|secret|password|passwd|token|authorization|auth.?json|private.?key|access.?key)", re.I)
_BACKENDS = {"manual", "claude", "codex", "api"}
_PROTOCOLS = {"responses", "chat_completions", "anthropic"}
_REGION_SOURCES = {"CN": {"boss", "nowcoder", "watchlist"}, "HK": {"linkedin", "jobsdb", "watchlist"}}


def _fail(message: str) -> None:
    # 配置值可能包含用户提供的凭据，错误只描述字段，不回显值。
    raise ValueError(message)


def _validate_api_url(value: str) -> None:
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        if not url.scheme or not host or url.username is not None or url.password is not None or url.query or url.fragment:
            _fail("llm.base_url 必须是不含用户信息、query 或 fragment 的 URL")
        is_loopback = host.lower() == "localhost"
        if not is_loopback:
            try:
                is_loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                is_loopback = False
        if url.scheme != "https" and not (url.scheme == "http" and is_loopback):
            _fail("llm.base_url 仅允许 HTTPS；HTTP 只允许 loopback")
        if url.port is not None and not 1 <= url.port <= 65535:
            _fail("llm.base_url 端口无效")
    except ValueError as exc:
        if str(exc).startswith("llm.base_url"):
            raise
        _fail("llm.base_url URL 格式无效")


def _check_sensitive_keys(value: Any, path: str = "config") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _SENSITIVE.search(str(key)):
                _fail("配置包含不允许保存的敏感字段: %s" % path)
            _check_sensitive_keys(child, path + "." + str(key))
    elif isinstance(value, list):
        for child in value:
            _check_sensitive_keys(child, path)
    elif isinstance(value, str) and re.search(r"(?i)(?:\bsk-[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}|\bBearer\s+\S+|\bAKIA[0-9A-Z]{16}\b)", value):
        _fail("配置含疑似凭据值，不能保存")


def _expect_keys(obj: Any, keys: set[str], path: str) -> dict:
    if not isinstance(obj, dict):
        _fail("配置字段格式错误: %s" % path)
    extra = set(obj) - keys
    missing = keys - set(obj)
    if extra:
        _fail("配置包含未知字段: %s" % path)
    if missing:
        _fail("配置缺少字段: %s" % path)
    return obj


def _integer(value: Any, path: str, low: int, high: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < low or (high is not None and value > high):
        _fail("配置数值无效: %s" % path)
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        _fail("配置格式无效: %s" % path)
    return value


def _defaults() -> dict:
    try:
        return yaml.safe_load((_RESOURCE / "default-config.yaml").read_text(encoding="utf-8"))
    except Exception:
        _fail("内置默认配置不可用")


def validate_config(obj: Any) -> dict:
    """严格校验 v1 配置并深拷贝归一；失败消息不包含配置值。"""
    _check_sensitive_keys(obj)
    _expect_keys(obj, {"version", "llm", "budget", "sources", "fetch", "prescore", "browser"}, "根")
    if obj["version"] != 1 or isinstance(obj["version"], bool):
        _fail("仅支持配置 schema version 1")
    c = copy.deepcopy(obj)
    _expect_keys(c["llm"], {"backend", "model", "protocol", "base_url", "credential", "concurrency", "timeout_sec", "reasoning_effort"}, "llm")
    llm = c["llm"]
    if llm["backend"] not in _BACKENDS: _fail("llm.backend 无效")
    if llm["protocol"] not in _PROTOCOLS: _fail("llm.protocol 无效")
    if llm["reasoning_effort"] not in {"low", "medium", "high", "xhigh"}: _fail("llm.reasoning_effort 无效")
    llm["model"] = _string(llm["model"], "llm.model").strip()
    llm["base_url"] = _string(llm["base_url"], "llm.base_url").strip()
    if llm["backend"] == "codex" and not llm["model"]:
        _fail("codex 后端必须配置模型")
    if llm["backend"] == "api":
        if not llm["model"] or not llm["base_url"]:
            _fail("api 后端必须配置 model 和 base_url")
        _validate_api_url(llm["base_url"])
    _expect_keys(llm["credential"], {"kind", "name"}, "llm.credential")
    cred = llm["credential"]
    if cred["kind"] not in {"env", "keyring", "session"}: _fail("llm.credential.kind 无效")
    cred["name"] = _string(cred["name"], "llm.credential.name").strip()
    if cred["kind"] == "env" and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cred["name"]):
        _fail("环境变量名称无效")
    if not cred["name"]: _fail("llm.credential.name 不能为空")
    llm["concurrency"] = _integer(llm["concurrency"], "llm.concurrency", 1, 8)
    llm["timeout_sec"] = _integer(llm["timeout_sec"], "llm.timeout_sec", 1, 3600)
    _expect_keys(c["budget"], {"max_jobs"}, "budget")
    c["budget"]["max_jobs"] = _integer(c["budget"]["max_jobs"], "budget.max_jobs", 1, 100)
    _expect_keys(c["sources"], {"CN", "HK"}, "sources")
    for region, allowed in _REGION_SOURCES.items():
        sources = c["sources"][region]
        if not isinstance(sources, list) or any(not isinstance(s, str) or s not in allowed for s in sources):
            _fail("sources.%s 只允许该地区支持的来源名称" % region)
        c["sources"][region] = list(dict.fromkeys(sources))
    _expect_keys(c["fetch"], {"CN", "HK", "watchlist", "rate", "cooldown_hours"}, "fetch")
    for region in ("CN", "HK"):
        section = c["fetch"][region]
        if not isinstance(section, dict): _fail("fetch.%s 格式无效" % region)
        if set(section) - _REGION_SOURCES[region]: _fail("fetch.%s 包含未知来源" % region)
        for source, options in section.items():
            if source == "watchlist":
                if options not in ({}, []): _fail("watchlist 查询配置独立保存在 profile/watchlist.yaml")
                continue
            blocks = options if isinstance(options, list) else [options]
            if not blocks or any(not isinstance(block, dict) for block in blocks):
                _fail("fetch.%s.%s 查询块格式无效" % (region, source))
            for block in blocks:
                allowed_keys = {"keywords", "city", "max_pages", "page_size", "detail_limit", "extra", "track"}
                if set(block) - allowed_keys: _fail("fetch.%s.%s 查询块含未知字段" % (region, source))
                keywords = block.get("keywords")
                if not isinstance(keywords, list) or any(not isinstance(k, str) for k in keywords) or not any(k.strip() for k in keywords):
                    _fail("fetch.%s.%s 必须配置非空 keywords" % (region, source))
                block["keywords"] = [k.strip() for k in keywords if k.strip()]
                if "city" in block and not isinstance(block["city"], str): _fail("fetch.%s.%s city 格式无效" % (region, source))
                for key in ("max_pages", "page_size", "detail_limit"):
                    if key in block: block[key] = _integer(block[key], "fetch.%s.%s.%s" % (region, source, key), 1, 1000)
                if "extra" in block and not isinstance(block["extra"], dict): _fail("fetch.%s.%s extra 格式无效" % (region, source))
                if block.get("track", "intern") not in {"intern", "campus"}: _fail("fetch.%s.%s track 无效" % (region, source))
    _expect_keys(c["fetch"]["watchlist"], {"max_pages", "detail_limit"}, "fetch.watchlist")
    c["fetch"]["watchlist"]["max_pages"] = _integer(c["fetch"]["watchlist"]["max_pages"], "fetch.watchlist.max_pages", 1, 100)
    c["fetch"]["watchlist"]["detail_limit"] = _integer(c["fetch"]["watchlist"]["detail_limit"], "fetch.watchlist.detail_limit", 1, 1000)
    if not isinstance(c["fetch"]["rate"], dict): _fail("fetch.rate 格式无效")
    for source, bounds in c["fetch"]["rate"].items():
        if source not in {"boss", "nowcoder", "linkedin", "jobsdb", "watchlist"} or not isinstance(bounds, list) or len(bounds) != 2:
            _fail("fetch.rate 格式无效")
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or x < 0 for x in bounds) or bounds[1] < bounds[0]:
            _fail("fetch.rate 数值无效")
    c["fetch"]["cooldown_hours"] = _integer(c["fetch"]["cooldown_hours"], "fetch.cooldown_hours", 1, 720)
    for region in ("CN", "HK"):
        for source in c["sources"][region]:
            if source != "watchlist" and source not in c["fetch"][region]:
                _fail("已启用来源缺少查询关键词")
    _expect_keys(c["prescore"], {"top_n", "min_overlap"}, "prescore")
    c["prescore"]["top_n"] = _integer(c["prescore"]["top_n"], "prescore.top_n", 1, 1000)
    c["prescore"]["min_overlap"] = _integer(c["prescore"]["min_overlap"], "prescore.min_overlap", 0, 1000)
    _expect_keys(c["browser"], {"profile"}, "browser")
    c["browser"]["profile"] = _string(c["browser"]["profile"], "browser.profile").strip()
    if c["browser"]["profile"] and not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", c["browser"]["profile"]):
        _fail("browser.profile 只能使用英文、数字、点、连字符或下划线")
    return c


class Workspace:
    def __init__(self, path=None):
        selected = path if path is not None else os.environ.get("JP_WORKSPACE")
        self.root = pathlib.Path(selected).expanduser().resolve() if selected else pathlib.Path(
            user_data_dir("job-pipeline", appauthor=False)).resolve()
        self.config_path = self.root / "config.yaml"

    def initialize(self) -> "Workspace":
        self.root.mkdir(parents=True, exist_ok=True)
        data = self.root / "data"
        profile = self.root / "profile"
        data.mkdir(exist_ok=True)
        profile.mkdir(exist_ok=True)
        if not self.config_path.exists():
            self.config_path.write_text(yaml.safe_dump(_defaults(), allow_unicode=True, sort_keys=False), encoding="utf-8")
        defaults = {
            profile / "constraints.yaml": """CN:\n  max_days_per_week: 7\n  days_negotiable: false\n  max_min_months: 99\n  employment_types: [internship, parttime, fulltime, unknown]\n  visa_ok: [none, local_permit_required, unknown]\n  unrestricted_locations: true\n  locations: [unrestricted]\n  regex_kill: []\n  regex_flag: []\nHK:\n  max_days_per_week: 7\n  days_negotiable: false\n  max_min_months: 99\n  employment_types: [internship, parttime, fulltime, unknown]\n  visa_ok: [none, local_permit_required, unknown]\n  unrestricted_locations: true\n  locations: [unrestricted]\n  regex_kill: []\n  regex_flag: []\n""",
            profile / "watchlist.yaml": "watchlist: []\n",
            data / "facts_index.json": "[]\n",
        }
        for path, text in defaults.items():
            if not path.exists(): path.write_text(text, encoding="utf-8")
        return self

    def load(self) -> dict:
        try:
            raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        except Exception:
            _fail("配置文件无法读取或解析")
        return validate_config(raw)

    def runtime_config(self) -> dict:
        c = self.load()
        root = self.root
        region_fetch = {}
        for region in ("CN", "HK"):
            configured = c["fetch"][region]
            region_fetch[region] = {source: copy.deepcopy(configured.get(source, {})) for source in c["sources"][region]}
        llm = copy.deepcopy(c["llm"])
        api = {"protocol": llm["protocol"], "base_url": llm["base_url"], "model": llm["model"], "concurrency": llm["concurrency"], "timeout_sec": llm["timeout_sec"], "reasoning_effort": llm["reasoning_effort"], "key_env": llm["credential"]["name"] if llm["credential"]["kind"] == "env" else "", "credential_kind": llm["credential"]["kind"], "credential_name": llm["credential"]["name"]}
        llm.update({"api": api, "codex_model": llm["model"], "codex_reasoning_effort": llm["reasoning_effort"], "codex_concurrency": llm["concurrency"], "codex_job_budget": c["budget"]["max_jobs"], "codex_timeout_sec": llm["timeout_sec"]})
        return {"llm": llm, "budget": copy.deepcopy(c["budget"]), "sources": copy.deepcopy(c["sources"]), "fetch": {**region_fetch, "watchlist": copy.deepcopy(c["fetch"]["watchlist"]), "rate": copy.deepcopy(c["fetch"]["rate"]), "cooldown_hours": c["fetch"]["cooldown_hours"]}, "prescore": copy.deepcopy(c["prescore"]), "browser": copy.deepcopy(c["browser"]), "paths": {"db": str(root / "data" / "jobs.sqlite3"), "tasks_dir": str(root / "tasks"), "facts_index": str(root / "data" / "facts_index.json"), "candidate_profile": str(root / "profile" / "candidate_profile.json"), "constraints": str(root / "profile" / "constraints.yaml"), "watchlist": str(root / "profile" / "watchlist.yaml"), "facts_dir": str(root / "facts"), "resume_repo": str(root / "resume-repo")}}


def resolve_credentials(cfg: dict, session_key: Optional[str] = None) -> Optional[str]:
    """从环境变量、可选 keyring 或当前会话解析密钥；不会读写 auth.json。"""
    try:
        llm = cfg.get("llm", {})
        cred = llm.get("credential", {})
        api = llm.get("api", {})
        kind = cred.get("kind") or api.get("credential_kind")
        name = cred.get("name") or api.get("credential_name") or api.get("key_env", "")
        if kind == "env":
            value = os.environ.get(name)
        elif kind == "keyring":
            try:
                import keyring  # optional dependency
                service, sep, account = name.partition(":")
                if not sep or not service or not account:
                    _fail("系统凭据库名称应为 service:account")
                value = keyring.get_password(service, account)
            except Exception:
                _fail("系统凭据库不可用")
        elif kind == "session":
            value = session_key or api.get("_session_key")
        else:
            _fail("凭据配置无效")
        if not value:
            _fail("未找到所选凭据；请检查环境变量、系统凭据库或当前会话")
        return value
    except ValueError:
        raise
    except Exception:
        _fail("凭据解析失败")


def configure(workspace: Workspace, candidate: dict, tester: Optional[Callable[[dict], Any]] = None) -> dict:
    """先验证并可选测试，再备份有效旧配置，最后原子替换。"""
    clean = validate_config(candidate)
    tested = False
    if tester is not None:
        try:
            candidate_runtime = _runtime_for(workspace, clean)
            result = tester(candidate_runtime)
        except Exception:
            _fail("配置测试失败；配置未更改")
        if not isinstance(result, dict) or result.get("status") != "configured" or result.get("tested") is not True:
            _fail("配置测试未确认成功；配置未更改")
        tested = True
    workspace.root.mkdir(parents=True, exist_ok=True)
    old_bytes = None
    if workspace.config_path.exists():
        try:
            old_bytes = workspace.config_path.read_bytes()
            old_cfg = validate_config(yaml.safe_load(old_bytes.decode("utf-8")))
        except Exception:
            _fail("现有配置无效；为保护数据，未覆盖")
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=workspace.root, prefix=".config-", suffix=".tmp", delete=False) as tmp:
            temp_name = tmp.name
            tmp.write(yaml.safe_dump(clean, allow_unicode=True, sort_keys=False))
            tmp.flush()
            os.fsync(tmp.fileno())
        if old_bytes is not None:
            backup = workspace.config_path.with_suffix(".yaml.bak")
            backup.write_bytes(old_bytes)
        os.replace(temp_name, workspace.config_path)
    except Exception:
        if old_bytes is not None:
            try:
                workspace.config_path.write_bytes(old_bytes)
            except Exception:
                pass
        _fail("配置保存失败；已尝试恢复原配置")
    finally:
        if temp_name and os.path.exists(temp_name):
            try: os.unlink(temp_name)
            except OSError: pass
    return {"version": clean["version"], "backend": clean["llm"]["backend"], "model": clean["llm"]["model"], "credential": {"kind": clean["llm"]["credential"]["kind"], "configured": True}, "sources": copy.deepcopy(clean["sources"]), "tested": tested, "saved": True}


def _runtime_for(workspace: Workspace, c: dict) -> dict:
    """生成候选运行配置，不经磁盘读取当前配置。"""
    region_fetch = {region: {source: copy.deepcopy(c["fetch"][region].get(source, {})) for source in c["sources"][region]} for region in ("CN", "HK")}
    llm = copy.deepcopy(c["llm"])
    llm.update({"api": {"protocol": llm["protocol"], "base_url": llm["base_url"], "model": llm["model"], "concurrency": llm["concurrency"], "timeout_sec": llm["timeout_sec"], "reasoning_effort": llm["reasoning_effort"], "key_env": llm["credential"]["name"] if llm["credential"]["kind"] == "env" else "", "credential_kind": llm["credential"]["kind"], "credential_name": llm["credential"]["name"]}, "codex_model": llm["model"], "codex_reasoning_effort": llm["reasoning_effort"], "codex_concurrency": llm["concurrency"], "codex_job_budget": c["budget"]["max_jobs"], "codex_timeout_sec": llm["timeout_sec"]})
    root = workspace.root
    return {"llm": llm, "budget": copy.deepcopy(c["budget"]), "sources": copy.deepcopy(c["sources"]), "fetch": {**region_fetch, "watchlist": copy.deepcopy(c["fetch"]["watchlist"]), "rate": copy.deepcopy(c["fetch"]["rate"]), "cooldown_hours": c["fetch"]["cooldown_hours"]}, "prescore": copy.deepcopy(c["prescore"]), "browser": copy.deepcopy(c["browser"]), "paths": {"db": str(root / "data" / "jobs.sqlite3"), "tasks_dir": str(root / "tasks"), "facts_index": str(root / "data" / "facts_index.json"), "candidate_profile": str(root / "profile" / "candidate_profile.json"), "constraints": str(root / "profile" / "constraints.yaml"), "watchlist": str(root / "profile" / "watchlist.yaml"), "facts_dir": str(root / "facts"), "resume_repo": str(root / "resume-repo")}}


def agent_prompt(workspace: Workspace) -> str:
    project = pathlib.Path(__file__).resolve().parents[1]
    guide = project / "docs" / "agent-setup.md"
    rules = project / "AGENTS.md"
    packaged_guide = _RESOURCE / "agent-setup.md"
    packaged_rules = _RESOURCE / "AGENTS.md"
    if not guide.exists() and packaged_guide.exists():
        guide = packaged_guide
    if not rules.exists() and packaged_rules.exists():
        rules = packaged_rules
    template = (_RESOURCE / "agent-prompt.txt").read_text(encoding="utf-8")
    return template.format(project_path=str(project), guide_path=str(guide), rules_path=str(rules), workspace_path=str(workspace.root))
