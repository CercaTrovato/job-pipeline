from __future__ import annotations
import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from jp import ingest, progress, sentinel
from jp.adapters import BROWSER_SOURCES, get_adapter
from jp.adapters.base import AdapterError, AuthRequiredError, RateLimiter, RiskControlError, SearchQuery
from jp.adapters.opencli import doctor_ok
from jp.models import RawJob


@dataclass
class FetchReport:
    source: str
    status: str                 # done / partial（部分查询块失败）/ skipped / cooldown / auth_required / error
    new: int = 0
    seen: int = 0
    merged: int = 0
    error: str = ""


def check_no_percent(q: SearchQuery, region: str, source: str) -> None:
    """opencli 在 Windows 下是 npm 的 .cmd 垫片，经 cmd.exe 时 '%NAME%' 会被当环境变量展开（已验证：
    'before %OS% after' → 'before Windows_NT after'）。keywords/city/extra 会流入 run_opencli 的 CLI 参数
    （LinkedIn）与 browser_eval 的 JS（Boss、牛客），含 '%' 就有被 cmd.exe 篡改的风险，直接拒绝。"""
    values = list(q.keywords) + [q.city] + [str(v) for v in q.extra.values()]
    for v in values:
        if "%" in v:
            raise ValueError("config.yaml fetch.%s.%s 的 keywords/city/extra 不能含 '%%'（Windows 下会被 cmd.exe "
                             "当环境变量展开）: %r" % (region, source, v))


def source_blocks(cfg: Dict[str, Any], region: str, source: str) -> List[Dict[str, Any]]:
    """一个来源可以配一个查询块（dict）或多个（list）：例如 Boss / 牛客各配实习块 + 校招块、深圳块 + 成都块。"""
    c = cfg["fetch"][region][source]
    return list(c) if isinstance(c, list) else [c]


def _block_query(block: Dict[str, Any], region: str, source: str) -> SearchQuery:
    q = SearchQuery(region=region, keywords=[str(k) for k in block["keywords"]], city=str(block.get("city") or ""),
                    max_pages=int(block.get("max_pages", 3)), page_size=int(block.get("page_size", 15)),
                    detail_limit=int(block.get("detail_limit", 40)), extra=dict(block.get("extra") or {}))
    check_no_percent(q, region, source)
    return q


def build_queries(cfg: Dict[str, Any], region: str, source: str) -> List[SearchQuery]:
    return [_block_query(b, region, source) for b in source_blocks(cfg, region, source)]


def build_query(cfg: Dict[str, Any], region: str, source: str) -> SearchQuery:
    """第一个查询块（单块配置即整个来源）。"""
    return build_queries(cfg, region, source)[0]


def make_limiter(cfg: Dict[str, Any], source: str) -> RateLimiter:
    lo, hi = (cfg["fetch"].get("rate") or {}).get(source, [3, 6])
    return RateLimiter(float(lo), float(hi))


MAX_DETAIL_ATTEMPTS = 2


def known_platform_ids(conn, source: str) -> set:
    """该来源已入库且已有详情的 platform_id（含并入其它主记录的 job_sources），供适配器跳过已知岗位的详情抓取。
    之前详情被预筛跳过、只有列表字段的行不算"已知"，下次抓取会补全（ingest 识别 detail_skipped 后升级）。"""
    ids, list_only = set(), set()
    for pid, raw_json in conn.execute("SELECT platform_id, raw_json FROM jobs WHERE source=? AND platform_id != ''", (source,)):
        if raw_json and '"detail_skipped"' in raw_json:
            try:
                raw = json.loads(raw_json)
            except ValueError:
                raw = {}
            # 详情被预筛跳过或读取失败的行：最多再试 MAX_DETAIL_ATTEMPTS 次（ingest 每次同样失败会 +1）
            if raw.get("detail_skipped") in ("hard_prefilter", "error") and int(raw.get("detail_attempts") or 0) < MAX_DETAIL_ATTEMPTS:
                list_only.add(pid)
                continue
        ids.add(pid)
    ids |= {r[0] for r in conn.execute("SELECT platform_id FROM job_sources WHERE source=? AND platform_id != ''", (source,))}
    return ids - list_only


def runtime_queries(cfg: Dict[str, Any], conn, region: str, source: str, constraints: Optional[Dict[str, Any]]):
    """每个查询块 → (SearchQuery, 轨道)。配置查询（已过 '%' 检查）+ 运行时键：known_ids（增量）与该轨道的天数 / 月数上限
    （适配器可据此省详情预算；校招块用 campus 覆盖后的上限）。"""
    known = known_platform_ids(conn, source)
    out = []
    for block in source_blocks(cfg, region, source):
        q = _block_query(block, region, source)
        track = str(block.get("track") or "intern")
        q.extra["known_ids"] = known
        if constraints and region in constraints:
            from jp.rules import hard
            c = hard.effective(constraints, region, track)
            if "max_min_months" in c:
                q.extra["max_min_months"] = c["max_min_months"]
            if "max_days_per_week" in c and not c.get("days_negotiable"):    # 天数可谈时不能因 5 天就省掉详情（那样 JD 只剩列表字段）
                q.extra["max_days_per_week"] = c["max_days_per_week"]
        out.append((q, track))
    return out


def runtime_query(cfg: Dict[str, Any], conn, region: str, source: str, constraints: Optional[Dict[str, Any]]) -> SearchQuery:
    return runtime_queries(cfg, conn, region, source, constraints)[0][0]


def run_fetch(cfg: Dict[str, Any], conn, region: str, sources: List[str], root, log: Callable[[str], None] = print,
              adapter_factory: Callable[[str], Any] = get_adapter, watchlist_runner: Optional[Callable[..., List[RawJob]]] = None,
              browser_check: Callable[[], Any] = doctor_ok, constraints: Optional[Dict[str, Any]] = None) -> List[FetchReport]:
    """逐个来源：冷却检查 → 限速器 → 适配器 search → ingest → runs 记录。任何适配器异常只记录不抛出。"""
    reports: List[FetchReport] = []
    browser_ok = True
    need_browser = [s for s in sources if s in BROWSER_SOURCES]
    if need_browser:
        browser_ok, detail = browser_check()
        if not browser_ok:
            log("浏览器未就绪：请打开 Edge（OpenCLI 扩展所在的 profile）后重跑；本次跳过 %s。详情：%s"
                % ("、".join(need_browser), ((detail or "").strip().splitlines() or [""])[0]))
    for source in sources:
        if source != "watchlist" and source not in (cfg["fetch"].get(region) or {}):
            log("%s 未在 config.yaml fetch.%s 里配置，跳过" % (source, region))
            reports.append(FetchReport(source, "skipped", error="该地区未配置此来源"))
            continue
        if source in BROWSER_SOURCES and not browser_ok:
            reports.append(FetchReport(source, "skipped", error="浏览器未就绪"))
            continue
        cd = sentinel.is_cooling(conn, source)
        if cd is not None:
            msg = "冷却中（至 %s）：%s" % (cd["until"], cd["reason"])
            log("%s %s" % (source, msg))
            reports.append(FetchReport(source, "cooldown", error=msg))
            continue
        limiter = make_limiter(cfg, source)
        with progress.Run(conn, "fetch", source=source) as run:
            block_errors: List[str] = []
            try:
                if source == "watchlist":
                    runner = watchlist_runner
                    if runner is None:
                        from jp.adapters.watchlist import run_watchlist
                        runner = run_watchlist

                    def on_company(done: int, total: int) -> None:
                        if run.pages_total != total:
                            run.set_total(pages=total)
                        run.tick(pages=1)
                    raws = runner(cfg, region, root, limiter, log, on_company)
                else:
                    blocks = runtime_queries(cfg, conn, region, source, constraints)
                    run.set_total(pages=len(blocks))
                    adapter = adapter_factory(source)
                    raws = []
                    for q, track in blocks:
                        if len(blocks) > 1:
                            log("%s 查询块：%s · %s · %s" % (source, q.city or "-", "校招" if track == "campus" else "实习", "、".join(q.keywords)))
                        try:
                            batch = adapter.search(q, limiter, log=log)
                        except (RiskControlError, AuthRequiredError):
                            raise                       # 风控 / 未登录是整个来源的事，照旧交给下面的处理
                        except AdapterError as e:
                            # 单个查询块失败（会话卡住之类）不该把前面几块已经抓到的几十条一起丢掉：记下来接着下一块。
                            block_errors.append(str(e))
                            log("%s 查询块失败，保留已抓到的 %d 条，继续下一块：%s" % (source, len(raws), e))
                            if len(blocks) > 1:
                                run.tick(pages=1)
                            continue
                        if track == "campus":
                            for rj in batch:
                                rj.raw["recruit_type"] = "校招"     # 校招块抓到的一律按校招轨道判（地点可成都、届别只认 2027）
                        raws.extend(batch)
                        if len(blocks) > 1:
                            run.tick(pages=1)
                    if block_errors and not raws:
                        raise AdapterError(block_errors[0])
            except RiskControlError as e:
                until = sentinel.start_cooldown(conn, source, str(e), hours=int(cfg["fetch"].get("cooldown_hours", 24)))
                run.cooldown(str(e))
                log("%s 触发风控哨兵，冷却至 %s：%s" % (source, until, e))
                reports.append(FetchReport(source, "cooldown", error="%s（冷却至 %s）" % (e, until)))
                continue
            except AuthRequiredError as e:
                run.fail(str(e))
                log("%s 未登录：%s" % (source, e))
                reports.append(FetchReport(source, "auth_required", error=str(e)))
                continue
            except AdapterError as e:
                run.fail(str(e))
                log("%s 失败：%s" % (source, e))
                reports.append(FetchReport(source, "error", error=str(e)))
                continue
            except ValueError as e:
                run.fail(str(e))
                log("%s 配置有误：%s" % (source, e))
                reports.append(FetchReport(source, "error", error=str(e)))
                continue
            r = ingest.ingest_jobs(conn, raws, source=source, region=region)
            run.tick(pages=0 if (source == "watchlist" or run.pages_total > 1) else 1, jobs_new=r.new, jobs_seen=r.seen)
            log("%s 完成：新增 %d / 已见 %d / 合并 %d / 补全详情 %d" % (source, r.new, r.seen, r.merged, r.upgraded))
            reports.append(FetchReport(source, "partial" if block_errors else "done", new=r.new, seen=r.seen,
                                       merged=r.merged, error="；".join(block_errors[:2])))
    return reports
