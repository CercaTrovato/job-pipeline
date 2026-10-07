"""同一份 JD 的重复帖与中介刷帖折叠。

牛客上大量岗位是同一份 JD 被挂在互不相关的公司名下（同一份"Ai应用开发"JD 同时挂在得物 / 网易 / 用友 /
4399 / 小红书名下，54 条），这是中介刷帖；华为那种"华为HUAWEI / 华为软件技术 / 上海华为技术"则是同一
雇主的不同主体，属于重复帖。两种都不该各占一次接口调用、各占一行待审，但也不能直接删——保留一条代表，
其余标 duplicate（看板可见、可重新入队），代表帖上记下"疑似刷帖"供人工判断。
"""
from __future__ import annotations
import difflib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from jp import db as jpdb
from jp import normalize as n
from jp.models import Status
from jp.rules import hard

MIN_JD_CHARS = 150          # 更短的 JD（"详见官网""岗位职责：招聘中"）不足以判重
SPAM_MIN_EMPLOYERS = 3      # 同一份 JD 落在 3 家及以上互不相关的公司名下 → 疑似刷帖；2 家多半是同岗跨平台
_JD_PREFIX = 4000           # 只比前 4000 字，避免长 JD 末尾的免责声明差异影响判重
NEAR_JD_RATIO = 0.98        # 近似帖只在标题、轨道、城市和关键条件也一致时折叠
_TITLE_CITIES = re.compile(
    r"Hong Kong|Shenzhen|Chengdu|Shanghai|Beijing|Guangzhou|Hangzhou|Singapore|London|Sydney|"
    r"深圳|成都|上海|北京|广州|杭州|香港|东莞|珠海|苏州|武汉|南京|西安|重庆|天津|厦门|合肥|长沙|"
    r"郑州|青岛|大连|宁波|无锡|佛山|昆明|沈阳|福州|济南|南昌|南宁|澳门|台北|九龙|观塘|沙田|中环|湾仔",
    re.IGNORECASE,
)
_TITLE_BRACKET = re.compile(r"[（(【\[]([^）)】\]]+)[）)】\]]")
_NON_LOCATION_BRACKET = re.compile(r"急招|热招|高薪|紧急招聘|校招|实习|应届|社招|(?:20)?2\d届", re.IGNORECASE)

# 判"是不是同一家公司"时要忽略的泛用词：它们出现在大量公司名里，共用它们不说明有关系
_GENERIC = set("""
科技 信息 网络 技术 集团 数据 智能 有限 公司 国际 电子 软件 服务 文化 传媒 咨询 股份 实业 互联 发展 投资 创新
研究 管理 商贸 通信 教育 医疗 生物 金融 证券 银行 保险 工程 设备 制造 材料 能源 环保 建设 贸易 电器 电气 系统
平台 产业 控股 中心 研究院 有限公司 分公司 深圳 上海 北京 广州 成都 杭州 香港 中国
group technology technologies holdings holding global international digital solutions systems services consulting
limited company china shenzhen shanghai beijing guangzhou chengdu hong kong asia pacific
""".split())

# 选代表帖：越靠前越优先（已推进 / 已人工处理的不动，未分析的最先被折叠）
_KEEP_RANK = {
    Status.SUBMITTED: 0, Status.FORM_FILLED: 0, Status.RESUME_READY: 0, Status.NEEDS_VARIANT: 0,
    Status.APPROVED: 0, Status.LATER: 1, Status.PENDING_REVIEW: 2, Status.MATCHED: 3,
    Status.ANALYZED: 4, Status.QUEUED: 5, Status.FETCHED: 6, Status.PRESCREENED_OUT: 7,
    Status.REJECTED_HARD: 8, Status.ADAPTER_BROKEN: 9, Status.SKIPPED: 1, Status.DUPLICATE: 11,
}
# 只有这些状态会被改成 duplicate：人工已裁决（投 / 放弃 / 稍后）与已推进投递的一律不动
_MUTABLE = {Status.FETCHED, Status.PRESCREENED_OUT, Status.REJECTED_HARD,
            Status.QUEUED, Status.ANALYZED, Status.MATCHED, Status.PENDING_REVIEW}


@dataclass
class Cluster:
    kind: str                       # same_employer（同一雇主的重复帖）/ multi_employer（疑似刷帖）
    keeper: Any
    others: List[Any] = field(default_factory=list)
    employers: int = 1

    @property
    def size(self) -> int:
        return len(self.others) + 1


@dataclass
class DedupResult:
    clusters: int = 0
    duplicates: int = 0     # 同一雇主重复帖被折叠的条数
    spam: int = 0           # 疑似刷帖被折叠的条数
    flagged: int = 0        # 被标注"疑似刷帖"的代表帖数


def jd_key(jd_text: str) -> Optional[str]:
    """JD 去空白后的前若干字；太短的返回 None（判不了重）。"""
    text = re.sub(r"\s+", "", jd_text or "").lower()
    return text[:_JD_PREFIX] if len(text) >= MIN_JD_CHARS else None


def _brand_tokens(name: str) -> set:
    """公司名里可能是"牌子"的片段：中文取相邻二字，英文取单词，去掉泛用词。"""
    s = n.norm_company(name)
    tokens = {w for w in re.findall(r"[a-z0-9]{3,}", s) if w not in _GENERIC}
    for run in re.findall(r"[一-鿿]+", s):
        if len(run) == 1:
            tokens.add(run)
        tokens.update(g for g in (run[i:i + 2] for i in range(len(run) - 1)) if g not in _GENERIC)
    return tokens


def same_employer(a: str, b: str) -> bool:
    """归一化同名，或共用一个非泛用的牌子词 → 当作同一雇主。

    华为HUAWEI / 上海华为技术 / 成都华为技术 共用"华为" → 同一雇主（重复帖）；
    上海得物信息 / 网易 / 用友 / 4399游戏 两两无共用词 → 不同公司（同 JD 即刷帖）。
    """
    if n.norm_company(a) == n.norm_company(b):
        return True
    return bool(_brand_tokens(a) & _brand_tokens(b))


def employer_families(names: Sequence[str]) -> List[List[str]]:
    """把公司名按"是不是同一家"归堆，返回每堆的名字列表。"""
    families: List[List[str]] = []
    for name in names:
        for family in families:
            if same_employer(family[0], name):
                family.append(name)
                break
        else:
            families.append([name])
    return families


def _city_key(row) -> str:
    """同一雇主按城市分组；地点缺失时保留标题里明确的城市或其他地点提示。"""
    city = n.norm_location(row["location"] if "location" in row.keys() else "")
    if city:
        return city
    title = row["title"] if "title" in row.keys() else ""
    cities = sorted({n.norm_location(m.group()) for m in _TITLE_CITIES.finditer(title or "")})
    if cities:
        return cities[0] if len(cities) == 1 else "title-cities:" + "|".join(cities)
    details = [re.sub(r"\s+", "", value).casefold() for value in _TITLE_BRACKET.findall(title or "")
               if not _NON_LOCATION_BRACKET.fullmatch(value.strip())]
    return "title-detail:" + "|".join(details) if details else "unknown"


def _keeper_key(row):
    rank = _KEEP_RANK.get(row["status"], 9)
    if json.loads(row["raw_json"] or "{}").get("dup_keep"):
        rank = max(rank, _KEEP_RANK[Status.DUPLICATE])   # 人工从 duplicate 捞回来的副本不顶掉原代表帖
    return (rank, -(row["prescore"] or 0.0), row["fetched_at"] or "", row["job_id"])


def _track(row) -> str:
    raw = json.loads(row["raw_json"] or "{}")
    if raw.get("track") in ("intern", "campus"):
        return raw["track"]
    return hard.detect_track(row["title"], row["jd_text"], raw.get("recruit_type") or "",
                             internship_fields=bool(raw.get("days_per_week") or raw.get("min_months")),
                             url=row["url"] if "url" in row.keys() else "")


def _title_markers(title: str) -> tuple:
    text = (title or "").lower()
    return tuple(k for k, pattern in (("fulltime", r"full[ -]?time|全职|全職"),
                                      ("parttime", r"part[ -]?time|兼职|兼職"))
                 if re.search(pattern, text))


def _critical_key(row) -> tuple:
    """相同文字也不能掩盖不同的到岗要求、届别、雇佣类型或薪酬。"""
    conditions = hard.job_conditions(row)
    return (_title_markers(row["title"]),
            tuple((k, conditions.get(k)) for k in ("days_per_week", "min_months", "class_year", "employment_type")),
            (row["salary_raw"] or "") if "salary_raw" in row.keys() else "",
            tuple(re.findall(r"\d+", row["jd_text"] or "")))


def _title_compatible(a, b) -> bool:
    left, right = n.norm_title(a["title"]), n.norm_title(b["title"])
    return bool(left and right and (left == right or
                (min(len(left), len(right)) >= 4 and difflib.SequenceMatcher(None, left, right).ratio() >= 0.65)))


def _spam_title_compatible(a, b) -> bool:
    """跨公司同 JD 只能在标题相同或一个只是另一标题的完整扩写时折叠。"""
    left, right = n.norm_title(a["title"]), n.norm_title(b["title"])
    return bool(left and right and (left == right or
                (min(len(left), len(right)) >= 4 and (left in right or right in left))))


def _raw_title_key(title: str) -> str:
    # 只删纯营销后缀；保留城市、职能和 (Full-Time)/(Part-Time) 等可改变申请决定的信息。
    text = _TITLE_BRACKET.sub(
        lambda m: "" if _NON_LOCATION_BRACKET.fullmatch(m.group(1).strip()) else m.group(), title or "")
    return re.sub(r"\s+", "", text).casefold()


def build_clusters(rows: Sequence[Any]) -> List[Cluster]:
    """按 JD 原文分组，返回需要折叠的簇（同一雇主的重复帖，或 ≥3 家公司共用一份 JD 的刷帖）。"""
    groups: Dict[tuple, List[Any]] = {}
    for row in rows:
        key = jd_key(row["jd_text"])
        if key:
            groups.setdefault((key, _track(row), _critical_key(row)), []).append(row)
    out: List[Cluster] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        families = employer_families([m["company"] or "" for m in members])
        if len(families) == 1:
            # 同一雇主：同一份 JD 常同时投在几个城市（海柔创新的项目助理岗 上海 / 北京 / 深圳），
            # 城市不同就是不同岗位，只折叠同城的那几条。
            by_city: Dict[str, List[Any]] = {}
            for m in members:
                by_city.setdefault(_city_key(m), []).append(m)
            batches = []
            for city_rows in by_city.values():
                title_groups: List[List[Any]] = []
                for row in sorted(city_rows, key=_keeper_key):
                    for title_group in title_groups:
                        if _title_compatible(row, title_group[0]):
                            title_group.append(row)
                            break
                    else:
                        title_groups.append([row])
                batches.extend(("same_employer", 1, g) for g in title_groups if len(g) > 1)
        elif len(families) >= SPAM_MIN_EMPLOYERS:
            # 同一份通用模板可能被不同城市、不同职能的真实岗位复用；只在同城且标题相容的子簇内判刷帖。
            batches = []
            by_city: Dict[str, List[Any]] = {}
            for m in members:
                by_city.setdefault(_city_key(m), []).append(m)
            for city_rows in by_city.values():
                title_groups: List[List[Any]] = []
                for row in sorted(city_rows, key=_keeper_key):
                    for title_group in title_groups:
                        if _spam_title_compatible(row, title_group[0]):
                            title_group.append(row)
                            break
                    else:
                        title_groups.append([row])
                for title_group in title_groups:
                    employer_count = len(employer_families([m["company"] or "" for m in title_group]))
                    if employer_count >= SPAM_MIN_EMPLOYERS:
                        batches.append(("multi_employer", employer_count, title_group))
        else:
            continue            # 两家不同名公司共用一份 JD：多半是同一岗位跨平台或换了签约主体，两条都留
        for kind, employers, batch in batches:
            ordered = sorted(batch, key=_keeper_key)
            out.append(Cluster(kind=kind, keeper=ordered[0], others=ordered[1:], employers=employers))
    out.sort(key=lambda c: c.keeper["job_id"])
    return out


def build_near_clusters(rows: Sequence[Any]) -> List[Cluster]:
    """只折叠同雇主、同城、同轨道、同原标题且关键条件一致的极近似 JD。"""
    groups: Dict[tuple, List[Any]] = {}
    for row in rows:
        key = jd_key(row["jd_text"])
        if key and row["status"] != Status.DUPLICATE:
            bucket = (_raw_title_key(row["title"]), _city_key(row), _track(row), _critical_key(row))
            groups.setdefault(bucket, []).append(row)
    out: List[Cluster] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        batches: List[List[Any]] = []
        for row in sorted(members, key=_keeper_key):
            key = jd_key(row["jd_text"])
            for batch in batches:
                keeper = batch[0]
                other = jd_key(keeper["jd_text"])
                if not same_employer(row["company"], keeper["company"]):
                    continue
                if 2 * min(len(key), len(other)) / (len(key) + len(other)) < NEAR_JD_RATIO:
                    continue
                if difflib.SequenceMatcher(None, key, other).ratio() >= NEAR_JD_RATIO:
                    batch.append(row)
                    break
            else:
                batches.append([row])
        out.extend(Cluster("near_employer", batch[0], batch[1:]) for batch in batches if len(batch) > 1)
    return out


def cluster_flags(raw: Dict[str, Any]) -> List[str]:
    """代表帖要在看板上展示的软标注（prescore / recheck 重写 soft_flags 时要并进去）。"""
    info = raw.get("dup_cluster") or {}
    if info.get("kind") == "multi_employer":
        return ["疑似刷帖：同一 JD 挂在 %d 家公司名下" % info.get("employers", 0)]
    return []


def run(conn) -> DedupResult:
    """折叠重复帖 / 刷帖。幂等：已是 duplicate 的不会被重复计数，也不会被选成代表帖。"""
    res = DedupResult()
    select = ("SELECT job_id, company, title, location, url, jd_text, salary_raw, status, prescore, fetched_at, "
              "raw_json FROM jobs")
    rows = conn.execute(select).fetchall()

    def apply_cluster(cluster: Cluster) -> None:
        folded = 0
        for row in cluster.others:
            if row["status"] not in _MUTABLE:
                continue
            raw = json.loads(row["raw_json"] or "{}")
            if raw.get("dup_keep"):
                continue        # 人工在看板上把它从 duplicate 重新入队过，不再折叠
            raw["dup_of"] = cluster.keeper["job_id"]
            raw["dup_reason"] = ("疑似刷帖：同一 JD 挂在 %d 家公司名下" % cluster.employers
                                 if cluster.kind == "multi_employer" else
                                 "近似重复帖：同公司同城同岗位，JD 和关键条件一致" if cluster.kind == "near_employer" else
                                 "重复帖：与代表帖同公司同 JD")
            conn.execute("UPDATE jobs SET raw_json=? WHERE job_id=?", (jpdb.json_dumps(raw), row["job_id"]))
            jpdb.set_status(conn, row["job_id"], Status.DUPLICATE)
            # 该帖原先可能是完全相同 JD 的代表帖；把旧副本的指针直接指向新代表。
            for child in conn.execute("SELECT job_id, raw_json FROM jobs WHERE status=?", (Status.DUPLICATE,)):
                child_raw = json.loads(child["raw_json"] or "{}")
                if child_raw.get("dup_of") == row["job_id"]:
                    child_raw["dup_of"] = cluster.keeper["job_id"]
                    conn.execute("UPDATE jobs SET raw_json=? WHERE job_id=?",
                                 (jpdb.json_dumps(child_raw), child["job_id"]))
            folded += 1
        if cluster.kind == "multi_employer":
            res.spam += folded
        else:
            res.duplicates += folded
        if folded:
            res.clusters += 1
        if cluster.kind == "multi_employer" and cluster.keeper["status"] in _MUTABLE:
            raw = json.loads(cluster.keeper["raw_json"] or "{}")
            info = {"kind": cluster.kind, "employers": cluster.employers, "size": cluster.size}
            if raw.get("dup_cluster") != info:
                raw["dup_cluster"] = info
                raw["soft_flags"] = [f for f in (raw.get("soft_flags") or []) if not f.startswith("疑似刷帖")]
                raw["soft_flags"] += cluster_flags(raw)
                conn.execute("UPDATE jobs SET raw_json=? WHERE job_id=?",
                             (jpdb.json_dumps(raw), cluster.keeper["job_id"]))
                res.flagged += 1

    for cluster in build_clusters(rows):
        apply_cluster(cluster)
    for cluster in build_near_clusters(conn.execute(select).fetchall()):
        apply_cluster(cluster)
    conn.commit()
    return res
