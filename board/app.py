from __future__ import annotations
import hashlib
import pathlib
import secrets

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, send_file, url_for

from jp import db as jpdb
from jp import browse, decide, duplicate_coverage, ingest, progress, sentinel
from jp.models import RawJob, Status
from jp.rules import hard


def create_app(db_path, schema_path, service=None) -> Flask:
    app = Flask(__name__, template_folder=str(pathlib.Path(__file__).parent / "templates"))
    app.config["DB_PATH"] = str(db_path)
    app.config["SECRET_KEY"] = secrets.token_hex(32)
    app.config.update(SESSION_COOKIE_SAMESITE="Strict", MAX_CONTENT_LENGTH=6 * 1024 * 1024)
    app.jinja_env.globals["csrf_token"] = lambda: ""
    app.config["PUBLIC_SERVICE"] = service

    _init_conn = jpdb.connect(app.config["DB_PATH"])
    jpdb.init_db(_init_conn, schema_path)
    _init_conn.close()

    def conn():
        if "conn" not in g:
            g.conn = jpdb.connect(app.config["DB_PATH"])
        return g.conn

    @app.teardown_appcontext
    def _close(exc):
        c = g.pop("conn", None)
        if c is not None:
            c.close()

    @app.context_processor
    def _inject():
        c = conn()
        counts = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
        return {"counts": {"pending": counts.get("pending_review", 0),
                           "selected": counts.get("approved", 0),
                           "queue": counts.get("queued", 0) + counts.get("analyzed", 0),
                           "prescreened": counts.get("fetched", 0) + counts.get("prescreened_out", 0)
                                          + counts.get("rejected_hard", 0) + counts.get("duplicate", 0)},
                "active": request.endpoint,
                "cooldowns": sentinel.active(c)}

    @app.route("/favicon.ico")
    def favicon():
        return send_file(pathlib.Path(__file__).parent / "icon.ico", mimetype="image/x-icon")

    @app.route("/")
    def index():
        return render_template("index.html", selected=False, **browse.list_jobs(conn(), "pending", request.args))

    @app.route("/selected")
    def selected():
        return render_template("index.html", selected=True, **browse.list_jobs(conn(), "selected", request.args))

    @app.route("/selected/duplicates")
    def selected_duplicates():
        return render_template("selected_duplicates.html", pairs=decide.approved_duplicate_pairs(conn()))

    @app.route("/selected/duplicates/review", methods=["POST"])
    def selected_duplicates_review():
        try:
            decide.review_approved_duplicate(conn(), request.form["job_id"], request.form["related_id"],
                                             request.form["action"], request.form.get("note", ""))
            flash("重复审查决定已记录")
        except (KeyError, ValueError) as e:
            flash(str(e))
        return redirect(url_for("selected_duplicates"))

    @app.route("/queue")
    def queue():
        return render_template("queue.html", **browse.list_jobs(conn(), "queue", request.args))

    @app.route("/job/<job_id>")
    def job(job_id):
        try:
            d = decide.job_detail(conn(), job_id)
        except KeyError:
            abort(404)
        req_by_id = {r["id"]: r for r in (d["analysis"] or {}).get("atomic_requirements", [])}
        back = browse.safe_return(request.args.get("return_to"))
        return render_template("job.html", d=d, req_by_id=req_by_id, return_to=back,
                               return_label="返回粗筛淘汰" if back.startswith("/prescreened") else
                               ("返回已选待投递" if back.startswith("/selected") else
                                ("返回待分析队列" if back.startswith("/queue") else "返回待挑选")))

    @app.route("/decide/<job_id>", methods=["POST"])
    def do_decide(job_id):
        try:
            new = decide.apply(conn(), job_id, request.form["decision"], request.form.get("note", ""),
                               confirm_duplicate=request.form.get("confirm_duplicate") == "yes")
            flash("已裁决: %s" % new)
        except decide.DuplicateConfirmationRequired as e:
            job = jpdb.get_job(conn(), job_id)
            related = [(item, jpdb.get_job(conn(), item["related_job_id"])) for item in e.conflicts]
            return render_template("confirm_selection.html", job=job, related=related,
                                   note=request.form.get("note", ""),
                                   return_to=browse.safe_return(request.form.get("return_to")))
        except ValueError as e:
            flash(str(e))
        return redirect(browse.safe_return(request.form.get("return_to")))

    @app.route("/referral", methods=["GET", "POST"])
    def referral():
        if request.method == "POST":
            f = request.form
            url = f.get("url", "").strip()
            jd_text = f.get("jd_text", "").strip()
            seed = url or jd_text   # 没有链接时用 JD 全文做种子，避免多条裸粘贴撞同一个 job_id
            raw = RawJob(platform_id=hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12] if seed else "",
                         title=f.get("title", "").strip() or "(待抽取)", company=f.get("company", "").strip() or "(待抽取)",
                         url=url or "referral:no-url", jd_text=jd_text, location="")
            if not raw.jd_text:
                flash("JD 不能为空")
                return redirect(url_for("referral"))
            r = ingest.ingest_jobs(conn(), [raw], source="referral", region=f.get("region", "CN"))
            flash("已入库 %s（新增 %d / 已见 %d）。下一步在设置的运行页面启动分析。" % (r.job_ids[0], r.new, r.seen))
            return redirect(url_for("referral"))
        return render_template("referral.html")

    @app.route("/runs")
    def runs():
        return render_template("runs.html", rows=progress.recent(conn(), 10))

    @app.route("/runs.json")
    def runs_json():
        return jsonify([dict(r) for r in progress.recent(conn(), 10)])

    @app.route("/prescreened")
    def prescreened():
        return render_template("prescreened.html", **browse.list_jobs(conn(), "prescreened", request.args))

    @app.route("/duplicate-coverage")
    def duplicate_coverage_page():
        constraints = hard.load_constraints(service.config()["paths"]["constraints"] if service else
                                           pathlib.Path(__file__).parents[1] / "tests" / "fixtures" / "constraints.yaml")
        return render_template("duplicate_coverage.html",
                               clusters=duplicate_coverage.audit_clusters(conn(), constraints))

    @app.route("/requeue/<job_id>", methods=["POST"])
    def requeue(job_id):
        try:
            decide.requeue(conn(), job_id, confirm_distinct=request.form.get("confirm_distinct") == "yes")
            rank = next((r["queue_rank"] for r in decide.queue_rows(conn()) if r["job_id"] == job_id), None)
            flash("已重新入队，当前顺序第 %s 位；待手动运行分析。" % (rank or "?"))
            previous = browse.safe_return(request.form.get("return_to"), "/prescreened")
            suffix = previous[len("/prescreened"):] if previous.startswith("/prescreened") else ""
            return redirect(url_for("queue") + suffix + "#job-" + job_id)
        except decide.DuplicateConfirmationRequired as e:
            job = jpdb.get_job(conn(), job_id)
            related = [(item, jpdb.get_job(conn(), item["related_job_id"])) for item in e.conflicts]
            return render_template("confirm_requeue.html", job=job, related=related,
                                   allow_override=e.allow_override,
                                   return_to=browse.safe_return(request.form.get("return_to"), "/prescreened"))
        except ValueError as e:
            flash(str(e))
        return redirect(browse.safe_return(request.form.get("return_to"), "/prescreened"))

    if service:
        from board.setup import install
        install(app, service)
    return app
