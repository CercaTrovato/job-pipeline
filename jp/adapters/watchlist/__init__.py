from __future__ import annotations
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import yaml

from jp.adapters.base import AdapterError, RateLimiter, RiskControlError, keyword_hit
from jp.adapters.watchlist.registry import FETCHERS
from jp.models import RawJob

REGION_RE = {
    "HK": re.compile(r"hong\s*kong|\bhk\b|香港|kowloon|九龍|九龙|新界", re.I),
    "CN": re.compile(r"shenzhen|深圳|\bchina\b|中国", re.I),
}


@dataclass
class Entry:
    company: str
    region: str
    tier: str
    grade: str
    careers_url: str
    ats: str
    keywords: List[str]
    enabled: bool
    params: Dict[str, Any] = field(default_factory=dict)
    note: str = ""


def load_watchlist(path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def entries(doc: Dict[str, Any], region: str) -> List[Entry]:
    """该地区、enabled 的条目；keywords 缺省取 keywords_default[region]。"""
    default_kw = (doc.get("keywords_default") or {}).get(region) or []
    out: List[Entry] = []
    for w in doc.get("watchlist") or []:
        if w.get("region") != region or not w.get("enabled", True):
            continue
        out.append(Entry(company=str(w.get("company", "")), region=region, tier=str(w.get("tier", "")), grade=str(w.get("grade", "")),
                         careers_url=str(w.get("careers_url", "")), ats=str(w.get("ats", "generic")),
                         keywords=[str(k) for k in (w.get("keywords") or default_kw)], enabled=True,
                         params=dict(w.get("params") or {}), note=str(w.get("note") or "")))
    return out


def api_entries(doc: Dict[str, Any], region: str, fetchers: Optional[Dict[str, Any]] = None) -> List[Entry]:
    f = FETCHERS if fetchers is None else fetchers
    return [e for e in entries(doc, region) if e.ats in f]


def generic_entries(doc: Dict[str, Any], region: str, fetchers: Optional[Dict[str, Any]] = None) -> List[Entry]:
    f = FETCHERS if fetchers is None else fetchers
    return [e for e in entries(doc, region) if e.ats not in f]


def run_watchlist(cfg: Dict[str, Any], region: str, root, limiter: RateLimiter, log: Callable[[str], None] = print,
                  on_company: Optional[Callable[[int, int], None]] = None, http: Any = None,
                  fetchers: Optional[Dict[str, Any]] = None) -> List[RawJob]:
    """逐家调用对应 ATS 抓取器 → 按该家 keywords 初筛（标题 + JD）→ 打上 raw['watchlist']。
    单家 AdapterError 记日志跳过；RiskControlError 向上抛（整个 watchlist 冷却）。"""
    path = pathlib.Path(cfg["paths"]["watchlist"])
    if root is not None and not path.is_absolute():
        path = pathlib.Path(root) / path
    doc = load_watchlist(path)
    f = FETCHERS if fetchers is None else fetchers
    ents = api_entries(doc, region, f)
    limits = dict((cfg.get("fetch") or {}).get("watchlist") or {})
    if http is None:
        from jp.http import HttpClient
        http = HttpClient()
    out: List[RawJob] = []
    for i, e in enumerate(ents):
        try:
            jobs = f[e.ats](e, http, limits, limiter, log)
        except RiskControlError:
            raise
        except AdapterError as ex:
            log("watchlist %s（%s）失败：%s" % (e.company, e.ats, ex))
            jobs = []
        except (KeyError, TypeError, ValueError) as ex:
            log("watchlist %s（%s）配置错误：%r" % (e.company, e.ats, ex))
            jobs = []
        kept = [j for j in jobs if keyword_hit(j.title + "\n" + j.jd_text, e.keywords)]
        for j in kept:
            j.raw.setdefault("watchlist", {"company": e.company, "ats": e.ats, "tier": e.tier, "grade": e.grade})
        log("watchlist %s（%s）：%d 条，关键词命中 %d" % (e.company, e.ats, len(jobs), len(kept)))
        out.extend(kept)
        if on_company:
            on_company(i + 1, len(ents))
    return out


def plan_lines(doc: Dict[str, Any], region: str) -> List[str]:
    """没有接口抓取器的公司清单，给 agent 亲自浏览用（配合 docs/agent-browse.md）。"""
    gens = generic_entries(doc, region)
    lines = ["agent 亲自浏览清单（%s）：%d 家" % (region, len(gens))]
    for e in gens:
        note = ("  # " + e.note) if e.note else ""
        lines.append("- %s [%s/%s] %s  关键词: %s%s" % (e.company, e.tier, e.grade, e.careers_url, ", ".join(e.keywords), note))
    lines.append("浏览后整理成 RawJob JSON（见 docs/agent-browse.md），入库：pipeline.py ingest --source watchlist --region %s --file <json>" % region)
    return lines


# 注册各 ATS 抓取器（导入即注册）
from jp.adapters.watchlist import workday, bamboohr, pinpoint, feishu, zhiye, workatsea  # noqa: E402,F401
