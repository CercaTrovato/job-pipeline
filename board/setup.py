from __future__ import annotations

import copy
import hmac
import json
import pathlib
import secrets
from urllib.parse import urlsplit

import yaml
from filelock import FileLock, Timeout
from flask import abort, g, jsonify, render_template, request, session

from jp import diagnostics, facts_index, resume, settings
from jp.security import safe_error
from jp.tasks import TaskRunner


def install(app, service):
    runner = TaskRunner(service)
    app.extensions["task_runner"] = runner
    ws = service.workspace

    def csrf():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    app.jinja_env.globals["csrf_token"] = csrf

    @app.before_request
    def protect():
        host = urlsplit("http://" + request.host).hostname
        if host not in ("127.0.0.1", "localhost", "::1"):
            abort(403, "仅允许本机访问。")
        if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
            return
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
            abort(403, "请求来源不匹配。")
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            abort(403, "不接受跨站请求。")
        token = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
        if not session.get("csrf") or not hmac.compare_digest(session["csrf"], token):
            abort(403, "请刷新页面后重试。")
        # 长任务自身持有跨进程锁；停止请求必须仍可访问。
        if request.path.startswith("/api/tasks"):
            return
        lock = FileLock(str(ws.root / ".pipeline.lock"), timeout=0)
        try:
            lock.acquire()
        except Timeout:
            return jsonify(error="已有任务正在处理，请等待完成或先停止。"), 409
        g.write_lock = lock
        if request.is_json and not isinstance(request.get_json(), dict):
            raise ValueError("请求 JSON 必须是对象。")

    @app.teardown_request
    def unlock(_):
        lock = g.pop("write_lock", None)
        if lock:
            lock.release()

    @app.errorhandler(ValueError)
    def bad_input(exc):
        return jsonify(error=safe_error(exc)), 400

    @app.errorhandler(Timeout)
    def conflict(_):
        return jsonify(error="已有流水线任务运行，请等待或停止。"), 409

    @app.route("/setup")
    def setup_page():
        return render_template("setup.html", workspace=str(ws.root))

    @app.route("/api/settings", methods=["GET", "POST"])
    def configuration():
        if request.method == "GET":
            cfg = ws.load()
            try:
                available = bool(settings.resolve_credentials(cfg, session_key=service.session_key))
            except ValueError:
                available = False
            return jsonify(config=cfg, credential_available=available, workspace=str(ws.root),
                           agent_prompt=settings.agent_prompt(ws), diagnosis=diagnostics.check(service.config(), False))
        body = request.get_json() or {}
        candidate = settings.validate_config(body.get("config"))
        new_key = body.get("session_key")
        if new_key is not None:
            if not isinstance(new_key, str) or not new_key.strip() or len(new_key) > 4096:
                raise ValueError("密钥输入无效。")
        previous_key = service.session_key
        test_key = new_key or previous_key

        def test(cfg):
            if test_key and candidate["llm"]["credential"]["kind"] == "session":
                cfg["llm"]["credential"] = {"kind": "session", "name": "current-session"}
                cfg["llm"]["api"].update(_session_key=test_key, credential_kind="session", credential_name="current-session")
            report = diagnostics.check(cfg, True)
            if report["status"] not in ("configured", "manual"):
                raise ValueError(report["message"])
            return report

        credential_backup = None
        keyring_changed = False
        if new_key:
            cred = candidate["llm"]["credential"]
            if cred["kind"] == "keyring":
                try:
                    import keyring
                    keyring_service, sep, keyring_account = cred["name"].partition(":")
                    if not sep or not keyring_service or not keyring_account:
                        raise ValueError("凭据名称应为 service:account。")
                    credential_backup = keyring.get_password(keyring_service, keyring_account)
                    keyring.set_password(keyring_service, keyring_account, new_key)
                    keyring_changed = True
                except Exception:
                    # 不将凭据库故障降级为明文；提示可选择当前会话方式。
                    raise ValueError("系统凭据库不可用，请选择当前会话输入，或设置环境变量。")
            elif cred["kind"] != "session":
                raise ValueError("环境变量方式请在系统环境设置密钥，或选择当前会话输入。")
        try:
            result = settings.configure(ws, candidate, test if body.get("test_model") else None)
        except Exception:
            if keyring_changed:
                if credential_backup:
                    keyring.set_password(keyring_service, keyring_account, credential_backup)
                else:
                    keyring.delete_password(keyring_service, keyring_account)
            raise
        if new_key:
            service.session_key = new_key if cred["kind"] == "session" else None
        return jsonify(result=result)

    @app.post("/api/model-test")
    def model_test():
        return jsonify(diagnostics.check(service.config(), True))

    @app.route("/api/profile", methods=["GET", "POST"])
    def profile():
        path = ws.root / "profile" / "resume-draft.json"
        if request.method == "GET":
            draft = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
            return jsonify(draft=draft, facts=facts_index.load(service.config()["paths"]["facts_index"]))
        if request.files.get("file"):
            file = request.files["file"]
            text = resume.extract_text(file.read(5 * 1024 * 1024 + 1), file.filename or "")
        else:
            text = (request.get_json() or {}).get("text", "")
            if not isinstance(text, str) or not text.strip() or len(text) > 200000:
                raise ValueError("请输入有效简历文本。")
        return jsonify(draft=resume.make_draft(ws, text, service.config(), False))

    @app.post("/api/profile/collect")
    def collect():
        return jsonify(draft=resume.collect_draft(ws))

    @app.post("/api/profile/confirm")
    def confirm():
        body = request.get_json() or {}
        accepted = resume.confirm_draft(ws, body.get("id"), body.get("items", []))
        return jsonify(accepted=len(accepted))

    @app.route("/api/sources", methods=["GET", "POST"])
    def sources():
        constraints_path = pathlib.Path(service.config()["paths"]["constraints"])
        watchlist_path = pathlib.Path(service.config()["paths"]["watchlist"])
        if request.method == "GET":
            return jsonify(constraints=yaml.safe_load(constraints_path.read_text(encoding="utf-8")),
                           watchlist=yaml.safe_load(watchlist_path.read_text(encoding="utf-8")))
        body = request.get_json() or {}
        cfg = ws.load()
        for key in ("sources", "fetch", "browser", "budget"):
            if key in body:
                cfg[key] = body[key]
        settings.validate_config(cfg)
        constraints = body.get("constraints")
        if constraints is not None:
            if not isinstance(constraints, dict) or set(constraints) != {"CN", "HK"}:
                raise ValueError("求职条件必须包含 CN 和 HK。")
            for region, item in constraints.items():
                if not isinstance(item, dict) or not isinstance(item.get("locations"), list) or any(
                        not isinstance(x, str) for x in item["locations"]):
                    raise ValueError("地点条件格式无效。")
                for key in ("max_days_per_week", "max_min_months"):
                    value = item.get(key)
                    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                        raise ValueError("到岗天数和时长必须是正整数。")
                if item["max_days_per_week"] > 7 or item["max_min_months"] > 99:
                    raise ValueError("到岗天数最大 7，时长最大 99。")
                if not isinstance(item.get("employment_types"), list) or not isinstance(item.get("visa_ok"), list):
                    raise ValueError("雇佣或签证条件格式无效。")
                for rule in item.get("regex_kill", []) + item.get("regex_flag", []):
                    import re
                    re.compile(rule["pattern"])
        watchlist = body.get("watchlist")
        if watchlist is not None:
            if not isinstance(watchlist, dict) or not isinstance(watchlist.get("watchlist"), list):
                raise ValueError("官网清单格式无效。")
            for item in watchlist["watchlist"]:
                if not isinstance(item, dict) or item.get("region") not in ("CN", "HK") or not item.get("company"):
                    raise ValueError("官网清单必须有公司名称及 CN/HK 地区。")
                if item.get("careers_url") and urlsplit(item["careers_url"]).scheme != "https":
                    raise ValueError("官网网址必须使用 HTTPS。")
        from jp.storage import atomic_text
        saved = {path: path.read_bytes() for path in (ws.config_path, constraints_path, watchlist_path)}
        try:
            result = settings.configure(ws, cfg)
            for path, value in ((constraints_path, constraints), (watchlist_path, watchlist)):
                if value is not None:
                    path.with_suffix(".yaml.bak").write_bytes(saved[path])
                    atomic_text(path, yaml.safe_dump(value, allow_unicode=True, sort_keys=False))
        except Exception:
            for path, original in saved.items():
                atomic_text(path, original.decode("utf-8"))
            raise
        return jsonify(result=result)

    @app.post("/api/sources/check")
    def source_check():
        return jsonify(checks=service.source_checks())

    @app.route("/api/tasks", methods=["GET", "POST"])
    def tasks():
        if request.method == "GET":
            return jsonify(tasks=runner.list())
        body = request.get_json() or {}
        return jsonify(id=runner.start(body.get("kind"), body.get("options", {}))), 202

    @app.post("/api/tasks/<tid>/stop")
    def stop(tid):
        runner.stop(tid)
        return jsonify(message="将在在途请求完成后停止，已完成结果保留。")

    @app.post("/api/tasks/<tid>/resume")
    def recover(tid):
        return jsonify(id=runner.resume(tid)), 202

    @app.post("/api/demo")
    def demo():
        return jsonify(service.demo())
