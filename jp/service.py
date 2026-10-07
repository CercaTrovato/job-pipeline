from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sqlite3
from dataclasses import asdict

from filelock import FileLock

from jp import db, decide, dedup, facts_index, ingest, packets, prescore, steps
from jp.models import RawJob, Status
from jp.rules import hard

RESOURCES = pathlib.Path(__file__).parent / "resources"


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class Stopped(Exception):
    pass


class Service:
    """CLI 与网页共用；所有资源随包分发，所有运行数据在用户工作区。"""

    def __init__(self, workspace, session_key=None):
        self.workspace = workspace
        self.session_key = session_key
        self.usage = {"packets": 0, "input_tokens": None, "cached_input_tokens": None, "output_tokens": None}
        workspace.initialize()
        self.lock = FileLock(str(workspace.root / ".pipeline.lock"), timeout=0, thread_local=False)
        with self.connect() as conn:
            db.init_db(conn, RESOURCES / "schema.sql")

    def connect(self):
        return db.connect(self.workspace.runtime_config()["paths"]["db"], factory=ClosingConnection)

    def config(self):
        cfg = self.workspace.runtime_config()
        if self.session_key:
            cfg["llm"]["api"]["_session_key"] = self.session_key
        return cfg

    def context(self, conn, cfg):
        return steps.Ctx(conn, pathlib.Path(cfg["paths"]["tasks_dir"]), RESOURCES / "llm" / "specs",
                         RESOURCES / "llm" / "schemas", facts_index.load(cfg["paths"]["facts_index"]),
                         hard.load_constraints(cfg["paths"]["constraints"]),
                         pathlib.Path(cfg["paths"]["candidate_profile"]))

    def dispatch(self, batch, cfg, stopped=lambda: False, stage=lambda *_: None):
        from jp import backends
        backend = cfg["llm"]["backend"]
        concurrency = cfg["llm"].get("codex_concurrency", 2)
        remaining = [p for p in batch if packets.status(p) != "done"]
        total = len(remaining)
        done = 0
        for offset in range(0, total, concurrency):
            if stopped():
                raise Stopped()
            chunk = remaining[offset:offset + concurrency]
            stage("模型处理", {"done": done, "total": total})
            if backend == "api":
                result = backends.run_api(chunk, backends.load_api_config(cfg))
            elif backend == "codex":
                result = backends.run_codex(chunk, timeout=cfg["llm"]["codex_timeout_sec"],
                                           model=cfg["llm"]["codex_model"],
                                           reasoning_effort=cfg["llm"]["codex_reasoning_effort"],
                                           concurrency=concurrency)
            else:
                return {"manual": True, "packets": total}
            if result.failed:
                raise RuntimeError("模型任务失败；请在模型设置检查接入方式，修正后恢复任务。")
            for packet in chunk:
                path = packet.dir / "usage.json"
                if path.exists():
                    item = json.loads(path.read_text(encoding="utf-8"))
                    self.usage["packets"] += 1
                    for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
                        if item.get(key) is not None:
                            self.usage[key] = (self.usage[key] or 0) + item[key]
            done += len(chunk)
        return {"done": done}

    def run(self, kind, options, stopped=lambda: False, stage=lambda *_: None):
        from jp.adapters.opencli import using_profile
        with using_profile(self.workspace.load()["browser"]["profile"]):
            self.usage = {"packets": 0, "input_tokens": None, "cached_input_tokens": None, "output_tokens": None}
            result = self._run(kind, options, stopped, stage)
            result["usage"] = self.usage
            return result

    def _run(self, kind, options, stopped=lambda: False, stage=lambda *_: None):
        cfg = self.config()
        if stopped():
            raise Stopped()
        if kind == "resume_generate":
            from jp import resume
            draft = resume.read_draft(self.workspace) if hasattr(resume, "read_draft") else json.loads(
                (self.workspace.root / "profile" / "resume-draft.json").read_text(encoding="utf-8"))
            stage("生成经历草稿", {})
            result = resume.make_draft(self.workspace, draft["text"], cfg, generate=True)
            return {"draft_status": result.get("status", "draft"), "manual": result.get("manual", False)}
        conn = self.connect()
        try:
            if kind in ("fetch", "pipeline"):
                from jp import fetch
                region = options.get("region", "CN")
                configured = self.workspace.load()["sources"][region]
                sources = options.get("sources") or configured
                if not sources:
                    raise ValueError("尚未启用岗位来源，请先在来源设置启用；已有岗位可选择只分析。")
                if any(source not in configured for source in sources):
                    raise ValueError("请先在来源设置启用并配置所选来源。")
                reports = []
                for source in sources:
                    if stopped():
                        raise Stopped()
                    stage("采集 " + source, {})
                    rows = fetch.run_fetch(cfg, conn, region, [source], self.workspace.root,
                                           log=lambda *_: None,
                                           constraints=hard.load_constraints(cfg["paths"]["constraints"]))
                    reports.extend(asdict(row) for row in rows)
                if kind == "fetch":
                    return {"sources": reports}
            ctx = self.context(conn, cfg)
            decide.release_later(conn)
            if not ctx.facts_entries:
                raise ValueError("请先在简历设置确认至少一条经历，再执行岗位匹配。")
            if not ctx.profile_path.exists() or json.loads(ctx.profile_path.read_text(encoding="utf-8")).get(
                    "facts_hash") != steps.facts_hash(ctx.facts_entries):
                stage("候选人画像", {})
                pp = steps.prepare_profile(ctx)
                state = self.dispatch([pp], cfg, stopped, stage)
                if state.get("manual"):
                    return state
                if not steps.collect_profile(ctx):
                    raise RuntimeError("候选人画像未通过事实引用校验。")
            job_ids = options.get("job_ids") or []
            budget = options.get("max_jobs", self.workspace.load()["budget"]["max_jobs"])
            if not isinstance(budget, int) or isinstance(budget, bool) or not 1 <= budget <= 100:
                raise ValueError("岗位预算必须为 1–100。")
            if not job_ids:
                stage("去重与筛选", {})
                dedup.run(conn)
                prescore.run(conn, ctx.facts_entries, ctx.constraints,
                             top_n=cfg["prescore"]["top_n"], min_overlap=cfg["prescore"]["min_overlap"])
                chosen = [row["job_id"] for row in decide.queue_rows(conn) if not row["blocking_approved"]][:budget]
            else:
                chosen = list(dict.fromkeys(job_ids))
                if len(chosen) > budget:
                    raise ValueError("指定岗位数量超过本轮预算。")
                rows = {row["job_id"]: row for row in decide.queue_rows(conn)}
                if any(jid not in rows or rows[jid]["blocking_approved"] for jid in chosen):
                    raise ValueError("指定岗位不在可处理队列，或与已选岗位冲突。")
            if not chosen:
                return {"jobs": 0, "message": "没有待处理岗位。"}
            ids = set(chosen)
            stage("分析岗位", {"jobs": len(ids)})
            result = self.dispatch(steps.prepare_analyze(ctx, ids), cfg, stopped, stage)
            if result.get("manual"):
                return result
            collected = steps.collect_analyze(ctx, ids)
            if collected.errors:
                raise RuntimeError("岗位分析未通过引用校验；任务包已保留。")
            if stopped():
                raise Stopped()
            stage("匹配证据", {"jobs": len(ids)})
            result = self.dispatch(steps.prepare_match(ctx, ids), cfg, stopped, stage)
            if result.get("manual"):
                return result
            collected = steps.collect_match(ctx, ids)
            if collected.errors:
                raise RuntimeError("岗位匹配未通过事实引用校验；任务包已保留。")
            return {"jobs": len(ids), "matched": collected.done}
        finally:
            conn.close()

    def demo(self):
        conn = self.connect()
        try:
            jobs = [RawJob(platform_id="demo-data", company="示例星河科技（虚构）", title="数据分析实习生",
                           url="https://example.com/jobs/data", location="示例城市",
                           jd_text="使用 Python 和 SQL 分析数据，制作业务指标图表。了解机器学习是加分项。"),
                    RawJob(platform_id="demo-ai", company="示例青山实验室（虚构）", title="AI 应用实习生",
                           url="https://example.com/jobs/ai", location="示例城市",
                           jd_text="使用 Python 实现检索增强问答。了解 RAG、模型评估和自动化测试。")]
            result = ingest.ingest_jobs(conn, jobs, "demo", "CN")
            for jid in result.job_ids:
                row = db.get_job(conn, jid)
                if row["status"] not in (Status.FETCHED, Status.QUEUED):
                    continue
                requirement = "使用 Python"
                analysis = {"summary": "离线演示岗位，分数是虚构样例。", "company": row["company"],
                            "title": row["title"], "atomic_requirements": [{"id": "R1", "quote": requirement,
                            "requirement": "Python", "level": "high", "kind": "skill"}],
                            "hard_conditions": {}, "flags": {}, "keywords": {"must": ["Python"], "core": [], "bonus": []}}
                match = {"items": [{"req_id": "R1", "evidence_grade": "C", "fact_ids": ["demo.python"],
                         "verdict": "partial", "gap_type": "evidence", "note": "演示：需要补充项目证据。"}],
                         "rationale": "演示结果，不是对用户的真实评估。"}
                conn.execute("INSERT OR REPLACE INTO analyses VALUES(?,?,?,?,?)",
                             (jid, "demo", db.json_dumps(analysis), "offline-demo", db.now_iso()))
                conn.execute("INSERT OR REPLACE INTO matches VALUES(?,?,?,?,?,?,?,?)",
                             (jid, "demo", 1, "[]", db.json_dumps(match), 60.0, 0, db.now_iso()))
                db.set_status(conn, jid, Status.PENDING_REVIEW)
            return {"new": result.new, "seen": result.seen}
        finally:
            conn.close()

    def source_checks(self):
        cfg = self.workspace.load()
        result = []
        conn = self.connect()
        try:
            from jp import sentinel
            from jp.adapters.opencli import doctor_ok
            enabled_browser = any(s in ("boss", "nowcoder", "linkedin") for ss in cfg["sources"].values() for s in ss)
            from jp.adapters.opencli import using_profile
            with using_profile(cfg["browser"]["profile"]):
                bridge = doctor_ok()[0] if enabled_browser and shutil.which("opencli") else False
            for region, sources in cfg["sources"].items():
                for source in ("boss", "nowcoder", "linkedin", "jobsdb", "watchlist"):
                    enabled = source in sources
                    cooling = sentinel.is_cooling(conn, source)
                    state = "disabled" if not enabled else "cooldown" if cooling else "needs_browser" if source in (
                        "boss", "nowcoder", "linkedin") and not bridge else "ready_to_try"
                    result.append({"region": region, "source": source, "status": state,
                                   "message": "未实际采集；账号登录和来源可用性将在采集时验证。"})
            return result
        finally:
            conn.close()
