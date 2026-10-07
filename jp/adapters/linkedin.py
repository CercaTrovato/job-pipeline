from __future__ import annotations
import re
from typing import Any, Callable, Dict, List

from jp.adapters.base import Adapter, AdapterError, AuthRequiredError, RateLimiter, RiskControlError, SearchQuery, split_for_detail
from jp.adapters.opencli import run_opencli
from jp.models import RawJob

_ID_RE = re.compile(r"(?:/jobs/view/|currentJobId=)(\d+)")
_PREFIX_RE = re.compile(r"^\s*(關於該職缺|关于该职位|About the job)\s*", re.I)
_PASSTHRU = ("experience-level", "job-type", "date-posted", "remote", "company")


def job_id_from_url(url: str) -> str:
    m = _ID_RE.search(url or "")
    return m.group(1) if m else ""


def to_rawjob(row: Dict[str, Any], detail: Dict[str, Any]) -> RawJob:
    """search 行（干净、英文）+ job-detail（只取 description / apply_url）→ RawJob。"""
    pid = job_id_from_url(row.get("url", "")) or job_id_from_url(detail.get("url", ""))
    desc = _PREFIX_RE.sub("", detail.get("description") or "").strip()
    return RawJob(
        platform_id=pid,
        title=row.get("title") or detail.get("title", ""),
        company=row.get("company") or detail.get("company") or "(待抽取)",
        url="https://www.linkedin.com/jobs/view/%s/" % pid if pid else row.get("url", ""),
        jd_text=desc,
        location=row.get("location", ""),
        salary_raw=row.get("salary", ""),
        posted_at=row.get("listed", ""),
        raw={"apply_url": detail.get("apply_url", ""), "workplace_type": detail.get("workplace_type", ""),
             "company_url": detail.get("company_url", ""), "list": row},
    )


class LinkedInAdapter(Adapter):
    """LinkedIn Jobs：`linkedin search`（--start 翻页）→ 逐条 `linkedin job-detail`。"""
    source = "linkedin"
    region = "HK"

    def __init__(self, run: Callable[..., Any] = run_opencli):
        self.run = run

    def search(self, q: SearchQuery, limiter: RateLimiter, log: Callable[[str], None] = print) -> List[RawJob]:
        rows: Dict[str, Dict[str, Any]] = {}
        for kw in q.keywords:
            for page in range(q.max_pages):
                limiter.wait()
                args = ["linkedin", "search", kw, "--limit", str(q.page_size), "--start", str(page * q.page_size),
                        "-f", "json", "--window", "background"]
                if q.city:
                    args += ["--location", q.city]
                for opt in _PASSTHRU:
                    if q.extra.get(opt):
                        args += ["--" + opt, str(q.extra[opt])]
                batch = self.run(args)
                if not isinstance(batch, list) or not batch:
                    break
                fresh = 0
                for r in batch:
                    pid = job_id_from_url(r.get("url", ""))
                    if pid and pid not in rows:
                        rows[pid] = r
                        fresh += 1
                log("linkedin search %r 第 %d 页：%d 条（新 %d）" % (kw, page + 1, len(batch), fresh))
                if fresh == 0 or len(batch) < q.page_size:
                    break
        out: List[RawJob] = []
        fetch, known, over = split_for_detail(rows, q.detail_limit, q.extra.get("known_ids"))
        for pid, r in known:                    # 已入库：不花一次 job-detail，回列表行让 ingest 记"已见"
            rj = to_rawjob(r, {})
            rj.raw["detail_skipped"] = "known"
            out.append(rj)
        failed = 0
        for pid, r in fetch:
            limiter.wait()
            try:
                d = self.run(["linkedin", "job-detail", "https://www.linkedin.com/jobs/view/%s/" % pid, "-f", "json", "--window", "background"])
            except (AuthRequiredError, RiskControlError):
                raise
            except AdapterError as e:            # 单条岗位页过期 / 改版（"could not find a job title"）：回退列表字段，不拖垮整个来源
                log("linkedin 详情失败，退回列表字段: %s（%s）" % (pid, str(e)[:120].replace("\n", " ")))
                rj = to_rawjob(r, {})
                rj.raw["detail_skipped"] = "error"
                out.append(rj)
                failed += 1
                continue
            if isinstance(d, list):
                d = d[0] if d else {}
            out.append(to_rawjob(r, d or {}))
        log("linkedin 详情 %d 条（失败退回 %d、已入库跳过 %d、超预算留待下次 %d）" % (len(fetch) - failed, failed, len(known), over))
        return out
