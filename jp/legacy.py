from __future__ import annotations
import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
RESOURCES = pathlib.Path(__file__).resolve().parent / "resources"
sys.path.insert(0, str(ROOT))

from jp import db as jpdb  # noqa: E402


def _conn(cfg):
    return jpdb.connect(ROOT / cfg["paths"]["db"])


def _ctx(cfg):
    from jp import steps, facts_index as fi
    from jp.rules import hard
    return steps.Ctx(conn=_conn(cfg), tasks_dir=ROOT / cfg["paths"]["tasks_dir"], specs_dir=RESOURCES / "llm" / "specs",
                     schemas_dir=RESOURCES / "llm" / "schemas", facts_entries=fi.load(ROOT / cfg["paths"]["facts_index"]),
                     constraints=hard.load_constraints(ROOT / cfg["paths"]["constraints"]),
                     profile_path=ROOT / cfg["paths"]["candidate_profile"])


def _dispatch(cfg, pks):
    """后端分派：api / codex 直接执行；claude / manual 停下等人填。"""
    if not pks:
        print("没有待填任务包")
        return
    backend = cfg["llm"]["backend"]
    from jp import backends, progress
    if backend == "api":
        api = backends.load_api_config(cfg)
        with progress.Run(_conn(cfg), "api", source=pks[0].step) as run:
            run.set_total(packets=len(pks))
            r = backends.run_api(pks, api, on_done=lambda: run.tick(packets=1))
        print("api 已执行 %d 个包，失败 %d" % (r.ok, r.failed))
    elif backend == "codex":
        ccfg = cfg["llm"]
        with progress.Run(_conn(cfg), "codex", source=pks[0].step) as run:
            run.set_total(packets=len(pks))
            r = backends.run_codex(pks, timeout=ccfg["codex_timeout_sec"],
                                   model=ccfg.get("codex_model", "gpt-5.6-luna"),
                                   reasoning_effort=ccfg.get("codex_reasoning_effort", "low"),
                                   concurrency=ccfg.get("codex_concurrency", 2),
                                   on_done=lambda: run.tick(packets=1))
        print("codex 已执行 %d 个包，失败 %d" % (r.ok, r.failed))
        if r.usage_reported:
            print("token 统计（%d/%d 包有回报）：输入 %d / 其中缓存 %d / 输出 %d"
                  % (r.usage_reported, len(pks), r.input_tokens, r.cached_input_tokens, r.output_tokens))
    else:
        print("已生成 %d 个任务包（后端 %s）。请填写各包 output.json 后运行: pipeline.py resume" % (len(pks), backend))
        for p in pks:
            print("  ", p.dir)


def cmd_profile_build(args, cfg):
    from jp import steps
    ctx = _ctx(cfg)
    _dispatch(cfg, [steps.prepare_profile(ctx)])


def cmd_analyze(args, cfg):
    from jp import steps
    ctx = _ctx(cfg)
    _dispatch(cfg, steps.prepare_analyze(ctx))


def cmd_match(args, cfg):
    from jp import steps
    ctx = _ctx(cfg)
    _dispatch(cfg, steps.prepare_match(ctx))


def cmd_packets(args, cfg):
    from jp import steps
    s = steps.pending_summary(ROOT / cfg["paths"]["tasks_dir"])
    if not s:
        print("没有待填任务包")
    for step, n in s.items():
        print("%-12s %d" % (step, n))


def cmd_resume(args, cfg):
    from jp import steps
    ctx = _ctx(cfg)
    if steps.collect_profile(ctx):
        print("候选人画像已更新:", ctx.profile_path)
    a = steps.collect_analyze(ctx)
    print("analyze 回收: %d 成功, %d 失败" % (a.done, len(a.errors)))
    for e in a.errors:
        print("  ", e)
    # 先回收已填好的 match 包，再生成新的：facts 索引变动会让 match 包重建，先回收可避免丢掉已填输出
    m = steps.collect_match(ctx)
    print("match 回收: %d 成功, %d 失败" % (m.done, len(m.errors)))
    for e in m.errors:
        print("  ", e)
    mp = steps.prepare_match(ctx)
    from jp import packets
    _dispatch(cfg, [p for p in mp if packets.status(p) in ("pending", "error")])


def cmd_init_db(args, cfg):
    conn = _conn(cfg)
    jpdb.init_db(conn, RESOURCES / "schema.sql")
    print("db 已初始化:", cfg["paths"]["db"])


def cmd_status(args, cfg):
    conn = _conn(cfg)
    rows = conn.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status ORDER BY n DESC").fetchall()
    if not rows:
        print("岗位库为空")
    for r in rows:
        print("%-18s %d" % (r["status"], r["n"]))


def cmd_facts_index(args, cfg):
    from jp import facts_index as fi
    src = pathlib.Path(cfg["paths"]["resume_repo"]) / "facts"
    entries = fi.build(src)
    fi.write(entries, ROOT / cfg["paths"]["facts_index"])
    print("facts 索引已生成: %d 条 -> %s" % (len(entries), cfg["paths"]["facts_index"]))


def cmd_ingest(args, cfg):
    from jp import ingest
    conn = _conn(cfg)
    raws = ingest.load_raw_file(args.file)
    r = ingest.ingest_jobs(conn, raws, source=args.source, region=args.region)
    print("ingest 完成: 新增 %d / 已见 %d / 合并 %d" % (r.new, r.seen, r.merged))
    for jid in r.job_ids:
        print(" ", jid)


def cmd_fetch(args, cfg):
    from jp import fetch as jfetch
    from jp.adapters import REGION_SOURCES
    conn = _conn(cfg)
    jpdb.init_db(conn, RESOURCES / "schema.sql")      # 幂等：保证 cooldowns 表存在
    from jp.rules import hard
    sources = args.source or REGION_SOURCES[args.region]
    constraints = hard.load_constraints(ROOT / cfg["paths"]["constraints"])   # 实习轨道的天数 / 月数上限传给适配器省详情预算
    reps = jfetch.run_fetch(cfg, conn, args.region, sources, ROOT, constraints=constraints)
    print("fetch 完成（%s）:" % args.region)
    for r in reps:
        print("  %-10s %-14s 新增 %d / 已见 %d / 合并 %d  %s" % (r.source, r.status, r.new, r.seen, r.merged, r.error))
    return reps


def cmd_watchlist_plan(args, cfg):
    from jp.adapters import watchlist as wl
    doc = wl.load_watchlist(ROOT / cfg["paths"]["watchlist"])
    for ln in wl.plan_lines(doc, args.region):
        print(ln)


def cmd_dedup(args, cfg):
    from jp import dedup
    r = dedup.run(_conn(cfg))
    print("dedup 完成: 折叠 %d 簇 / 同雇主重复帖 %d 条 / 疑似刷帖 %d 条 / 代表帖标注 %d 条"
          % (r.clusters, r.duplicates, r.spam, r.flagged))


def cmd_prescore(args, cfg):
    from jp import prescore, facts_index as fi, progress
    from jp.rules import hard
    conn = _conn(cfg)
    entries = fi.load(ROOT / cfg["paths"]["facts_index"])
    constraints = hard.load_constraints(ROOT / cfg["paths"]["constraints"])
    with progress.Run(conn, "prescore") as run:
        r = prescore.run(conn, entries, constraints, top_n=cfg["prescore"]["top_n"], min_overlap=cfg["prescore"]["min_overlap"])
        total = r.queued + r.out + r.killed + r.backlog
        run.set_total(packets=total)
        run.tick(packets=total, jobs_new=r.queued)
    print("prescore 完成: 进队 %d / 淘汰 %d / 硬伤 %d / 候补留在 fetched %d" % (r.queued, r.out, r.killed, r.backlog))


def _auto(cfg) -> bool:
    return cfg["llm"]["backend"] in ("api", "codex")


def cmd_run(args, cfg):
    if getattr(args, "fetch", False):
        if not args.region:
            raise SystemExit("run --fetch 需要 --region CN|HK")
        cmd_fetch(args, cfg)
    from jp import decide, duplicate_coverage, steps, packets
    from jp.rules import hard
    job_limit = getattr(args, "max_jobs", None)
    if job_limit is None:
        job_limit = cfg["llm"].get("codex_job_budget", 3) if cfg["llm"]["backend"] == "codex" else 3
    if job_limit is not None and job_limit < 1:
        raise SystemExit("--max-jobs 必须大于 0")
    if job_limit > 3:
        raise SystemExit("每轮最多处理 3 条岗位")
    target_ids = None
    requested_ids = list(dict.fromkeys(getattr(args, "job_ids", None) or []))
    if requested_ids and len(requested_ids) > job_limit:
        raise SystemExit("--job-id 数量不能超过本轮岗位上限")
    if not requested_ids:
        cmd_dedup(args, cfg)      # 自动模式先折叠重复帖，再从候补池补队列
        cmd_prescore(args, cfg)
    ctx = _ctx(cfg)
    if not requested_ids:
        constraints = hard.load_constraints(ROOT / cfg["paths"]["constraints"])
        promoted = duplicate_coverage.promote_eligible_representatives(ctx.conn, constraints, max_new=3)
        if promoted:
            print("可信且合格的未审重复簇代表入队: %d" % len(promoted))
    if requested_ids:
        available = {r["job_id"] for r in ctx.conn.execute(
            "SELECT job_id FROM jobs WHERE status IN ('analyzed','queued') AND job_id IN (%s)" %
            ",".join("?" for _ in requested_ids), requested_ids).fetchall()}
        missing = [job_id for job_id in requested_ids if job_id not in available]
        if missing:
            raise SystemExit("指定岗位不在分析队列: %s" % ", ".join(missing))
        blocked = [r for r in decide.queue_rows(ctx.conn)
                   if r["job_id"] in requested_ids and r["blocking_approved"]]
        if blocked:
            raise SystemExit("同招聘品牌已有相同 JD / 职位编号的已选岗位，跳过分析: %s"
                             % ", ".join(r["job_id"] for r in blocked))
        target_ids = set(requested_ids)
        print("本轮指定处理 %d 条岗位" % len(target_ids))
    elif job_limit is not None:
        processable = [r for r in decide.queue_rows(ctx.conn) if not r["blocking_approved"]]
        target_ids = {r["job_id"] for r in processable[:job_limit]}
        print("本轮最多处理 %d 条岗位（人工重新入队的优先）" % job_limit)
    n = decide.release_later(ctx.conn)
    if n:
        print("稍后到期回队: %d" % n)
    if steps.collect_profile(ctx):
        print("候选人画像已更新:", ctx.profile_path)
    for _ in range(3):
        a_pk = steps.prepare_analyze(ctx, target_ids)
        _dispatch(cfg, [p for p in a_pk if packets.status(p) in ("pending", "error")])
        if not _auto(cfg):
            return
        a = steps.collect_analyze(ctx, target_ids)
        print("analyze 回收: %d 成功, %d 失败" % (a.done, len(a.errors)))
        for e in a.errors:
            print("  ", e)
        m_pk = steps.prepare_match(ctx, target_ids)
        _dispatch(cfg, [p for p in m_pk if packets.status(p) in ("pending", "error")])
        m = steps.collect_match(ctx, target_ids)
        print("match 回收: %d 成功, %d 失败" % (m.done, len(m.errors)))
        for e in m.errors:
            print("  ", e)
        if not a_pk and not m_pk:
            break
        if a.done == 0 and m.done == 0:
            break


def cmd_recheck(args, cfg):
    from jp import steps
    r = steps.recheck_hard(_ctx(cfg))
    print("recheck 完成: 改为硬条件不合 %d / 内推仅标注 %d / 保持 %d / 从硬条件不合恢复 %d" % (r.rejected, r.annotated, r.kept, r.restored))


def cmd_review(args, cfg):
    from jp import decide
    conn = _conn(cfg)
    n = decide.release_later(conn)
    if n:
        print("稍后到期回队: %d" % n)
    rows = decide.pending_rows(conn)
    if not rows:
        print("没有待审批岗位")
    for r in rows:
        print("%s  %5.1f  %s | %s | %s  %s" % (r["job_id"], r["soft_score"], r["region"], r["company"], r["title"],
                                             "可改文案" if r["fixable"] else "缺口需补证据或能力"))
        print("      ", r["summary"])
        if r["top_gaps"]:
            print("       缺口:", "；".join(r["top_gaps"]))


def cmd_decide(args, cfg):
    from jp import decide
    try:
        print("->", decide.apply(_conn(cfg), args.job_id, args.decision, args.note or "",
                                 confirm_duplicate=args.confirm_duplicate))
    except decide.DuplicateConfirmationRequired as e:
        raise SystemExit("发现同品牌已选相似岗位 %s；核实后可加 --confirm-duplicate"
                         % ", ".join(item["related_job_id"] for item in e.conflicts))


def cmd_requeue(args, cfg):
    from jp import decide
    try:
        decide.requeue(_conn(cfg), args.job_id, confirm_distinct=args.confirm_distinct)
    except decide.DuplicateConfirmationRequired as e:
        raise SystemExit("同品牌同 JD 已选岗位 %s；仅有不同官网编号且核实独立后可加 --confirm-distinct"
                         % ", ".join(item["related_job_id"] for item in e.conflicts))
    print("-> queued")


def cmd_edit(args, cfg):
    from jp import db as jpdb
    fields = {k: v for k, v in (("company", args.company), ("title", args.title), ("location", args.location)) if v}
    jpdb.update_job_fields(_conn(cfg), args.job_id, **fields)
    print("已更新", args.job_id, fields)


def cmd_board(args, cfg):
    from board.app import create_app
    app = create_app(ROOT / cfg["paths"]["db"], RESOURCES / "schema.sql")
    print("看板: http://localhost:%d" % args.port)
    app.run(host="127.0.0.1", port=args.port, debug=False)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pipeline.py", description="求职投递半自动化流水线")
    p.add_argument("--config", default=str(ROOT / "config.yaml"))
    p.add_argument("--llm", choices=["api", "codex", "claude", "manual"], help="覆盖 config.yaml 的 llm.backend")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="建表").set_defaults(fn=cmd_init_db)
    sub.add_parser("status", help="各状态岗位数").set_defaults(fn=cmd_status)
    sub.add_parser("facts-index", help="从 resume-workflow/facts 生成脱敏索引").set_defaults(fn=cmd_facts_index)
    sub.add_parser("profile-build", help="生成候选人画像任务包").set_defaults(fn=cmd_profile_build)
    p_ing = sub.add_parser("ingest", help="RawJob JSON 入库")
    p_ing.add_argument("--source", required=True, help="boss|nowcoder|linkedin|jobsdb|referral|...")
    p_ing.add_argument("--region", required=True, choices=["CN", "HK"])
    p_ing.add_argument("--file", required=True)
    p_ing.set_defaults(fn=cmd_ingest)
    p_f = sub.add_parser("fetch", help="调适配器抓岗位并入库（受限速与风控哨兵约束）")
    p_f.add_argument("--region", required=True, choices=["CN", "HK"])
    p_f.add_argument("--source", action="append", choices=["boss", "nowcoder", "linkedin", "jobsdb", "watchlist"], help="可重复；缺省为该地区全部来源")
    p_f.set_defaults(fn=cmd_fetch)
    p_wp = sub.add_parser("watchlist-plan", help="列出需要 agent 亲自浏览的目标公司（无接口抓取器的）")
    p_wp.add_argument("--region", required=True, choices=["CN", "HK"])
    p_wp.set_defaults(fn=cmd_watchlist_plan)
    sub.add_parser("dedup", help="折叠同一份 JD 的重复帖与多公司刷帖").set_defaults(fn=cmd_dedup)
    sub.add_parser("prescore", help="确定性粗筛").set_defaults(fn=cmd_prescore)
    sub.add_parser("recheck", help="按当前硬条件规则（含抓取器字段与实习/校招轨道）重判队列与待审岗位").set_defaults(fn=cmd_recheck)
    sub.add_parser("analyze", help="为 queued 岗位生成 analyze 任务包").set_defaults(fn=cmd_analyze)
    sub.add_parser("match", help="为 analyzed 岗位生成 match 任务包").set_defaults(fn=cmd_match)
    sub.add_parser("packets", help="待填任务包统计").set_defaults(fn=cmd_packets)
    sub.add_parser("resume", help="回收已填任务包并推进").set_defaults(fn=cmd_resume)
    p_run = sub.add_parser("run", help="[fetch →] prescore → analyze → match 一条龙")
    p_run.add_argument("--region", choices=["CN", "HK"], help="配合 --fetch 指定地区")
    p_run.add_argument("--fetch", action="store_true", help="先抓取再分析")
    p_run.add_argument("--source", action="append", choices=["boss", "nowcoder", "linkedin", "jobsdb", "watchlist"])
    p_run.add_argument("--max-jobs", type=int, help="本轮最多处理多少条岗位；Codex 默认取 config.yaml 的 codex_job_budget")
    p_run.add_argument("--job-id", dest="job_ids", action="append", help="指定本轮处理的队列岗位 ID；可重复，仍受岗位上限约束")
    p_run.set_defaults(fn=cmd_run)
    sub.add_parser("review", help="列出待审批岗位").set_defaults(fn=cmd_review)
    p_dec = sub.add_parser("decide", help="裁决岗位")
    p_dec.add_argument("job_id"); p_dec.add_argument("decision", choices=["apply", "skip", "later"]); p_dec.add_argument("--note")
    p_dec.add_argument("--confirm-duplicate", action="store_true", help="确认仍选择同品牌相似岗位")
    p_dec.set_defaults(fn=cmd_decide)
    p_rq = sub.add_parser("requeue", help="粗筛/硬伤岗位重新入队"); p_rq.add_argument("job_id")
    p_rq.add_argument("--confirm-distinct", action="store_true", help="官网职位编号不同且已核实为独立岗位时允许同品牌同 JD 入队")
    p_rq.set_defaults(fn=cmd_requeue)
    p_ed = sub.add_parser("edit", help="修正岗位的公司/岗位名/地点"); p_ed.add_argument("job_id"); p_ed.add_argument("--company"); p_ed.add_argument("--title"); p_ed.add_argument("--location"); p_ed.set_defaults(fn=cmd_edit)
    p_b = sub.add_parser("board", help="启动本机看板"); p_b.add_argument("--port", type=int, default=5050); p_b.set_defaults(fn=cmd_board)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = jpdb.load_config(args.config)
    if args.llm:
        cfg["llm"]["backend"] = args.llm
    args.fn(args, cfg)


if __name__ == "__main__":
    main()
