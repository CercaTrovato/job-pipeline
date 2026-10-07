from __future__ import annotations
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml


@dataclass
class HardResult:
    passed: bool
    reasons: List[str] = field(default_factory=list)   # 硬条件不合的原因（淘汰）
    track: str = "intern"
    flags: List[str] = field(default_factory=list)     # 软标注（不淘汰，看板提示，如"要求每周 5 天（可谈）""可远程"）


def load_constraints(path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


# ---------- 轨道：实习 / 校招（毕业后入职） ----------

INTERN_RE = re.compile(r"实习|實習|\bintern(?:ship)?s?\b", re.I)
# 标题 / 招聘类型里的校招字样（强信号）
CAMPUS_RE = re.compile(r"校招|校园招聘|校園招聘|秋招|春招|提前批|应届|應屆|毕业生|畢業生|管培生|(?:20)?2\d\s*届|\bcampus\b"
                       r"|\bgraduate\s+(?:program(?:me)?|trainee|hire|scheme)s?\b|\bnew\s+grad(?:uate)?s?\b|\bfresh\s+grad(?:uate)?s?\b"
                       r"|\bmanagement\s+trainees?\b", re.I)
# JD 正文只认这些短语：实习 JD 里顺带一句"可获校招 offer"不算校招
CAMPUS_JD_RE = re.compile(r"校园招聘|校園招聘|秋季校招|春季校招|秋招|春招|应届毕业生|應屆畢業生|(?:20)?2\d\s*届(?:毕业|畢業|校招|应届|應屆|学生|學生|同学)"
                          r"|\bgraduate\s+(?:program(?:me)?|scheme|trainee)s?\b|\bcampus\s+recruit|\bmanagement\s+trainees?\b", re.I)
CAMPUS_URL_RE = re.compile(r"campus", re.I)
REMOTE_RE = re.compile(r"远程|遠程|在家办公|居家办公|线上实习|線上實習|\bremote\b|\bwfh\b|work\s+from\s+home", re.I)
REMOTE_FIELD_RE = re.compile(r"remote|遠距|远程|远端|在家", re.I)
RECRUIT_TYPE_MAP = {"实习": "internship", "實習": "internship", "兼职": "parttime", "兼職": "parttime",
                    "全职": "fulltime", "全職": "fulltime", "正式": "fulltime", "外包": "fulltime", "顾问": "fulltime"}
_LOC_SPLIT = re.compile(r"[，,、/;；|]")
# "26届 / 2026届 / 2026 届" 与英文 "Class of 2026 / graduating in 2026"
CLASS_YEAR_RE = re.compile(r"(?:20)?(2\d)\s*届|\b(?:class\s+of|graduat(?:ing|ion|e)\s+(?:in|by|year))\s*:?\s*(20\d\d)\b", re.I)
# "26届及以后 / 2026届或之后" 这类开放区间不判届别
OPEN_YEAR_RE = re.compile(r"届\s*(?:及|或|和)?\s*(?:以后|之后|以上|往后|后)|(?:20\d\d|\d\d\s*届)\s*(?:及以后|and\s+later|or\s+later|onwards?)", re.I)


def detect_track(title: str, jd_text: str = "", recruit_type: str = "", internship_fields: bool = False, url: str = "") -> str:
    """"intern"（实习）或 "campus"（校招 / 毕业后入职）。判定顺序：
    ① 标题含实习、招聘类型是实习、或抓取器给了到岗天数 / 最短月数（只有实习帖才有这两个字段）→ 实习；
    ② 招聘类型 / 标题含校招字样，或 URL 是校招站点（含 campus）→ 校招；
    ③ JD 正文出现校园招聘 / 应届毕业生 / 20xx 届毕业这类强短语 → 校招；否则默认实习。"""
    rt = (recruit_type or "").strip()
    if INTERN_RE.search(title or "") or rt in ("实习", "實習") or internship_fields:
        return "intern"
    if CAMPUS_RE.search(rt) or CAMPUS_RE.search(title or "") or CAMPUS_URL_RE.search(url or ""):
        return "campus"
    if CAMPUS_JD_RE.search(jd_text or ""):
        return "campus"
    return "intern"


def effective(constraints: Dict[str, Any], region: str, track: str = "intern") -> Dict[str, Any]:
    """该地区在该轨道下生效的约束：校招轨道用 `campus:` 块覆盖同名键。"""
    c = dict(constraints[region])
    if track == "campus" and c.get("campus"):
        c.update(c["campus"])
    return c


# ---------- 岗位级字段（抓取器给的结构化信息） ----------

def _raw(row) -> Dict[str, Any]:
    keys = row.keys() if hasattr(row, "keys") else ()
    if "raw" in keys and isinstance(row["raw"], dict):
        return row["raw"]
    if "raw_json" in keys and row["raw_json"]:
        try:
            return json.loads(row["raw_json"])
        except ValueError:
            return {}
    return {}


def split_location(loc: str) -> List[str]:
    return [x.strip() for x in _LOC_SPLIT.split(loc or "") if x.strip()]


def job_conditions(row) -> Dict[str, Any]:
    """从抓取器给的岗位级字段（地点 / 到岗天数 / 最短时长 / 招聘类型）构造部分硬条件；拿不到的键不出现。"""
    raw = _raw(row)
    hc: Dict[str, Any] = {}
    locs = split_location(row["location"] if "location" in row.keys() else "")
    if locs:
        hc["location"] = locs
    for k in ("days_per_week", "min_months"):
        v = raw.get(k)
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            hc[k] = v
    et = RECRUIT_TYPE_MAP.get(str(raw.get("recruit_type") or "").strip()) or _english_employment(raw, row)
    if et:
        hc["employment_type"] = et
    gy = CLASS_YEAR_RE.search(str(raw.get("graduation_year") or ""))      # 牛客校招帖的结构化届别："2027届" / "毕业不限"
    if gy:
        y = gy.group(1) or gy.group(2)
        hc["class_year"] = 2000 + int(y) if len(y) == 2 else int(y)
    return hc


_TITLE_STUDENT_RE = re.compile(r"intern|graduate|trainee|student|placement|实习|實習|校招|应届|應屆|管培", re.I)


def _english_employment(raw: Dict[str, Any], row) -> Optional[str]:
    """JobsDB `work_types`、Workday `time_type`、BambooHR `employment`、Pinpoint `employment_type` 这类英文雇佣类型字段：
    标题带 intern / graduate / trainee 的实习或校招帖不据此判断（香港暑期实习常标 Full time）；其余 Full time / Permanent /
    Contract 视为全职，Part time 视为兼职。"""
    vals: List[str] = []
    wt = raw.get("work_types")
    if isinstance(wt, list):
        vals += [str(x) for x in wt]
    for k in ("time_type", "employment", "employment_type"):
        if isinstance(raw.get(k), str) and raw[k]:
            vals.append(raw[k])
    text = " ".join(vals).lower()
    if not text:
        return None
    title = row["title"] if "title" in row.keys() else ""
    if _TITLE_STUDENT_RE.search(title or ""):
        return None
    if re.search(r"part[ _-]?time|casual", text):
        return "parttime"
    if re.search(r"full[ _-]?time|permanent|contract", text):
        return "fulltime"
    return None


def merge_conditions(model_hc: Optional[Dict[str, Any]], job_hc: Dict[str, Any]) -> Dict[str, Any]:
    """模型从 JD 抽的硬条件优先，抓取器字段只补空缺；地点取并集（任一命中即通过，避免总部地址之类误杀）。"""
    out = dict(model_hc or {})
    for k, v in job_hc.items():
        cur = out.get(k)
        if k == "location":
            out[k] = list(cur or []) + [x for x in v if x not in (cur or [])]
        elif cur in (None, [], "", "unknown"):
            out[k] = v
    return out


# ---------- 规则判定 ----------

def is_remote(row, raw: Dict[str, Any], analysis_flags: Optional[Dict[str, Any]] = None) -> bool:
    """岗位是否可远程：抓取器字段（LinkedIn workplace_type、JobsDB work_arrangement、watchlist workplace_type）、
    模型 flags.remote、或标题 / JD 出现远程字样。"""
    if analysis_flags and analysis_flags.get("remote"):
        return True
    for k in ("workplace_type", "work_arrangement"):
        v = raw.get(k)
        if isinstance(v, str) and REMOTE_FIELD_RE.search(v):
            return True
    keys = row.keys() if hasattr(row, "keys") else ()
    text = "%s\n%s" % (row["title"] if "title" in keys else "", row["jd_text"] if "jd_text" in keys else "")
    return bool(REMOTE_RE.search(text))


def evaluate(hard_conditions: Dict[str, Any], region: str, constraints: Dict[str, Any], track: str = "intern",
             remote: bool = False) -> HardResult:
    """规则判定硬条件。字段缺失（None / 空）一律视为未知 → 不淘汰。

    `days_negotiable: true`（用户 2026-09-22：每周 ≥5 天可谈）时超出天数上限只软标注不淘汰；
    `remote_locations`（用户：可远程的实习成都也行）在岗位可远程时并入允许地点。"""
    hc = hard_conditions
    c = effective(constraints, region, track)
    reasons: List[str] = []
    flags: List[str] = []

    locs = [_norm(x) for x in (hc.get("location") or [])]
    allowed = [_norm(x) for x in c["locations"]]
    if remote:
        allowed += [_norm(x) for x in (c.get("remote_locations") or [])]
        flags.append("可远程")
    if locs and not c.get("unrestricted_locations", False) and not any(any(a in l or l in a for a in allowed) for l in locs):
        reasons.append("地点不符: %s" % "/".join(hc["location"]))

    d = hc.get("days_per_week")
    if d is not None and d > c["max_days_per_week"]:
        if c.get("days_negotiable"):
            flags.append("要求每周 %d 天（可谈）" % d)
        else:
            reasons.append("要求每周 %d 天" % d)

    m = hc.get("min_months")
    if m is not None and m > c["max_min_months"]:
        reasons.append("要求 ≥%d 个月" % m)

    et = hc.get("employment_type") or "unknown"
    if et not in c["employment_types"]:
        reasons.append("雇佣类型不符: %s" % et)

    if hc.get("graduated_required") and c.get("check_graduated", True):
        reasons.append("要求已毕业")

    od = hc.get("onsite_days")
    if od is not None and od > c["max_days_per_week"]:
        if c.get("days_negotiable"):
            flags.append("要求现场 %d 天（可谈）" % od)
        else:
            reasons.append("要求现场 %d 天" % od)

    visa = hc.get("visa") or "unknown"
    if visa not in c["visa_ok"]:
        reasons.append("签证要求不符: %s" % visa)

    cy = hc.get("class_year")
    if cy and c.get("class_years") and int(cy) not in {int(y) for y in c["class_years"]}:
        reasons.append("届别不符: %d（要求 %s 届）" % (cy, "/".join(str(y) for y in c["class_years"])))

    return HardResult(passed=not reasons, reasons=reasons, track=track, flags=flags)


def class_year_reasons(text: str, class_years: List[int]) -> List[str]:
    """校招届别：文中写了届别且没有一个等于目标届 → 不符；开放区间（"26届及以后"）不判。"""
    found = set()
    for m in CLASS_YEAR_RE.finditer(text or ""):
        y = m.group(1) or m.group(2)
        found.add(2000 + int(y) if len(y) == 2 else int(y))
    if not found or found & {int(y) for y in class_years} or OPEN_YEAR_RE.search(text or ""):
        return []
    return ["届别不符: %s（要求 %s 届）" % ("/".join(str(y) for y in sorted(found)), "/".join(str(y) for y in class_years))]


def regex_prescreen(text: str, region: str, constraints: Dict[str, Any], track: str = "intern",
                    class_year_known: bool = False) -> List[str]:
    """文本级淘汰原因：`regex_kill` 命中 + 校招届别（抓取器给了结构化届别时不再从文本猜）。"""
    c = effective(constraints, region, track)
    hits: List[str] = []
    for rule in c.get("regex_kill") or []:
        if re.search(rule["pattern"], text, flags=re.IGNORECASE):
            hits.append(rule["reason"])
    if c.get("class_years") and not class_year_known:
        hits.extend(class_year_reasons(text, c["class_years"]))
    return hits


def regex_flags(text: str, region: str, constraints: Dict[str, Any], track: str = "intern") -> List[str]:
    """文本级软标注：`regex_flag` 命中只提示不淘汰（如"每周 5 天到岗"）。"""
    c = effective(constraints, region, track)
    return [rule["note"] for rule in (c.get("regex_flag") or []) if re.search(rule["pattern"], text, flags=re.IGNORECASE)]


def check_job(row, constraints: Dict[str, Any], analysis_hc: Optional[Dict[str, Any]] = None,
              analysis_flags: Optional[Dict[str, Any]] = None) -> HardResult:
    """岗位级硬条件总入口：判轨道 → 合并模型抽取与抓取器字段 → 规则判定（含远程放宽）→ 标题+JD 的文本正则、届别与软标注。

    prescore（模型尚未分析，analysis_hc=None）与 prepare_match / recheck 共用，保证同一岗位两处口径一致。
    """
    raw = _raw(row)
    job_hc = job_conditions(row)
    track = detect_track(row["title"], row["jd_text"], raw.get("recruit_type") or "",
                         internship_fields=("days_per_week" in job_hc or "min_months" in job_hc),
                         url=row["url"] if "url" in row.keys() else "")
    hc = merge_conditions(analysis_hc, job_hc)
    r = evaluate(hc, row["region"], constraints, track, remote=is_remote(row, raw, analysis_flags))
    text = "%s\n%s" % (row["title"] or "", row["jd_text"] or "")
    for reason in regex_prescreen(text, row["region"], constraints, track, class_year_known="class_year" in job_hc):
        if reason not in r.reasons:
            r.reasons.append(reason)
    for note in regex_flags(text, row["region"], constraints, track):
        if note not in r.flags:
            r.flags.append(note)
    r.passed = not r.reasons
    return r
