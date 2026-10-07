from __future__ import annotations
import json
import math
import re
import unicodedata
from collections import Counter
from urllib.parse import parse_qs, urlencode, urlsplit

from jp import decide
from jp.models import Status


SOURCE_NAMES = {"boss": "Boss 直聘", "nowcoder": "牛客", "linkedin": "LinkedIn",
                "jobsdb": "JobsDB", "watchlist": "公司官网", "referral": "内推"}
REGIONS = {"CN": "内地 CN", "HK": "香港 HK"}
STAGES = {"intern": "实习", "campus": "校招 / 毕业生", "unspecified": "标题未标明"}
TRACK_STAGE = {"intern": "intern", "campus": "campus"}     # 规则判定的轨道（prescore / recheck 写进 raw_json.track）
STATUSES = {"fetched": "候补（未分析）", "prescreened_out": "粗筛淘汰", "rejected_hard": "硬条件不合",
            "duplicate": "重复帖 / 疑似刷帖", "queued": "待分析", "analyzed": "待匹配"}
ROLE_RULES = [
    ("ai", "AI / 算法", r"算法|机器学习|深度学习|大模型|多模态|ai(?=应用|工程|研发|算法|软件|院)|\b(?:ai|llm|nlp|aigc|computer vision|machine learning|data scien\w*)\b"),
    ("data", "数据分析", r"数据分析|商业分析|数据科学|\b(?:data analy\w*|business analy\w*|data scien\w*|analytics|business intelligence)\b"),
    ("data-eng", "数据工程", r"数据开发|数据仓库|数据平台|数据工程|大数据|\bdata engineer\w*\b"),
    ("backend", "后端开发", r"后端|服务端|\b(?:back.?end|server.side|java|golang)\b"),
    ("frontend", "前端 / 客户端", r"前端|客户端|移动开发|\b(?:front.?end|android|ios|mobile|flutter)\b"),
    ("software", "软件开发", r"软件|全栈|研发|开发|\b(?:software|full.?stack|developer|development|coding)\b"),
    ("infra", "基础设施 / IT", r"运维|基础设施|系统|推理|训练框架|云原生|负载均衡|\b(?:infra\w*|devops|sre|platform|it|support engineer|network|cloud)\b"),
    ("product", "产品 / 项目", r"产品|项目管理|项目经理|\b(?:product|project|program) manager\b"),
    ("ops", "运营 / 内容", r"运营|内容|社区|开发者关系|\b(?:operations?|content|community|developer relations)\b"),
    ("design", "设计 / 创意", r"设计师|创意|\b(?:design\w*|ux|ui|creative)\b"),
    ("hardware", "硬件 / 机器人", r"硬件|电子|电气|机械|机器人|嵌入式|\b(?:hardware|robot\w*|embedded|electrical|mechanical)\b"),
    ("business", "商务 / 金融", r"商务|销售|渠道|金融|量化|投资|精算|解决方案|供应链|\b(?:sales|investment|quant\w*|actuar\w*|finance|financial|solutions?|supply chain|business development)\b"),
    ("corporate", "人事 / 行政", r"招聘|人事|人力|行政|财务|法务|\b(?:hr|talent|recruit\w*|admin\w*|legal|accountant)\b"),
]
ROLE_NAMES = {key: label for key, label, _ in ROLE_RULES}
ROLE_NAMES["other"] = "其他 / 待分类"


def _raw_track(raw_json):
    try:
        return (json.loads(raw_json or "{}") or {}).get("track")
    except ValueError:
        return None


def normalize(value):
    return unicodedata.normalize("NFKC", value or "").casefold()


def title_tags(title):
    text = normalize(title)
    roles = [key for key, _, pattern in ROLE_RULES if re.search(pattern, text)]
    # 职能词优先：招聘“大模型人才”不等于从事算法研发。
    nontech = {"corporate", "business", "product", "ops", "design"}
    if nontech.intersection(roles):
        roles = [r for r in roles if r != "ai"]
    # “软件产品经理”“开发者运营”不自动算软件开发岗。
    if nontech.intersection(roles):
        roles = [r for r in roles if r != "software"]
    if any(r in roles for r in ("backend", "frontend", "data-eng")):
        roles = [r for r in roles if r != "software"]
    stages = []
    if re.search(r"实习|實習|\bintern(?:ship)?s?\b", text):
        stages.append("intern")
    if re.search(r"校招|应届|應屆|毕业生|畢業生|\b(?:graduate|campus)\b", text):
        stages.append("campus")
    return roles or ["other"], stages or ["unspecified"]


FILTER_KEYS = ("q", "company", "region", "source", "stage", "status", "sort")


def list_jobs(conn, page, args, page_size=30):
    if page in ("pending", "selected"):
        rows = decide.pending_rows(conn, Status.PENDING_REVIEW if page == "pending" else Status.APPROVED)
        base = "/" if page == "pending" else "/selected"
    elif page == "queue":
        rows = decide.queue_rows(conn)
        base = "/queue"
    else:
        rows = [dict(r) for r in conn.execute(
            "SELECT job_id, company, title, region, source, status, prescore, url, location, jd_text, fetched_at, raw_json "
            "FROM jobs WHERE status IN ('fetched','prescreened_out','rejected_hard','duplicate') "
            "ORDER BY prescore DESC, job_id")]
        base = "/prescreened"
    for row in rows:
        row["roles"], row["stages"] = title_tags(row["title"])
        raw_json = row.pop("raw_json", None)
        track = row.pop("track", None) or _raw_track(raw_json)
        if track in TRACK_STAGE:
            row["stages"] = [TRACK_STAGE[track]]        # 规则轨道可靠于标题字样（牛客校招帖标题常无"校招"）
        if row.get("status") == Status.DUPLICATE:
            try:
                row["duplicate"] = decide.duplicate_info(conn, json.loads(raw_json or "{}"))
            except ValueError:
                row["duplicate"] = None
        row["search_text"] = normalize(" ".join(row.get(k) or "" for k in
                                                ("company", "title", "location", "jd_text", "summary")))
    filters = {k: args.get(k, "").strip() for k in FILTER_KEYS}
    filters["roles"] = list(dict.fromkeys(r for r in args.getlist("role") if r in ROLE_NAMES))
    if page in ("pending", "selected"):
        filters["status"] = ""
    if filters["sort"] not in (("queue", "score", "newest", "company") if page == "queue" else
                                ("score", "newest", "company")):
        filters["sort"] = "queue" if page == "queue" else "score"

    def matches(row, omit=None):
        if not all(word in row["search_text"] for word in normalize(filters["q"]).split()):
            return False
        if omit != "company" and normalize(filters["company"]) not in normalize(row["company"]):
            return False
        for key in ("region", "source", "status"):
            if key != omit and filters[key] and row.get(key) != filters[key]:
                return False
        if omit != "stage" and filters["stage"] and filters["stage"] not in row["stages"]:
            return False
        return omit == "roles" or not filters["roles"] or bool(set(filters["roles"]).intersection(row["roles"]))

    filtered = [r for r in rows if matches(r)]
    role_counts = Counter(role for r in rows if matches(r, "roles") for role in r["roles"])
    company_counts = Counter(r["company"] for r in rows if matches(r, "company"))
    facets = {}
    for key, labels in (("region", REGIONS), ("source", SOURCE_NAMES), ("stage", STAGES), ("status", STATUSES)):
        counts = Counter(value for r in rows if matches(r, key)
                         for value in (r["stages"] if key == "stage" else [r.get(key, "")]))
        choices = dict(labels)
        choices.update({v: labels.get(v, v) for v in counts if v})
        if filters[key]:
            choices.setdefault(filters[key], filters[key])
        facets[key] = [(v, label, counts[v]) for v, label in choices.items() if counts[v] or filters[key] == v]
    if page == "queue" and filters["sort"] == "queue":
        filtered.sort(key=lambda r: r["queue_rank"])
    elif filters["sort"] == "company":
        filtered.sort(key=lambda r: (normalize(r["company"]), normalize(r["title"]), r["job_id"]))
    elif filters["sort"] == "newest":
        filtered.sort(key=lambda r: (r.get("fetched_at") or "", r["job_id"]), reverse=True)
    else:
        score_key = "soft_score" if page in ("pending", "selected") else "prescore"
        filtered.sort(key=lambda r: (-(r.get(score_key) or 0), r["job_id"]))
    pages = max(1, math.ceil(len(filtered) / page_size))
    try:
        number = min(pages, max(1, int(args.get("page", 1))))
    except (ValueError, TypeError):
        number = 1

    def link(**changes):
        values = {k: v for k, v in filters.items() if k != "roles" and v}
        values["role"] = filters["roles"]
        values.update(changes)
        query = urlencode({k: v for k, v in values.items() if v}, doseq=True)
        return base + ("?" + query if query else "")

    active = []
    for key, label in (("q", "搜索"), ("company", "公司"), ("region", "地区"),
                       ("source", "来源"), ("stage", "招聘类型"), ("status", "淘汰类型")):
        if filters[key]:
            name = {"region": REGIONS, "source": SOURCE_NAMES, "stage": STAGES, "status": STATUSES}.get(key, {}).get(filters[key], filters[key])
            active.append((label + "：" + name, link(**{key: ""})))
    for role in filters["roles"]:
        active.append((ROLE_NAMES[role], link(role=[r for r in filters["roles"] if r != role])))
    return dict(rows=filtered[(number - 1) * page_size:number * page_size], filters=filters,
                total=len(rows), matched=len(filtered), page_number=number, pages=pages, base=base,
                list_url=link(page=number if number > 1 else None), active_filters=active,
                previous=link(page=number - 1) if number > 1 else None,
                next_page=link(page=number + 1) if number < pages else None,
                role_options=[(k, v, role_counts[k]) for k, v in ROLE_NAMES.items() if role_counts[k] or k in filters["roles"]],
                companies=sorted(company_counts.items(), key=lambda item: (-item[1], normalize(item[0]))),
                facets=facets, role_names=ROLE_NAMES, source_names=SOURCE_NAMES,
                stage_names=STAGES, status_names=STATUSES)


def safe_return(value, default="/"):
    """只允许本地列表及已知查询参数，不接受跨站跳转。"""
    try:
        parsed = urlsplit(value or "")
        if parsed.scheme or parsed.netloc or parsed.path not in ("/", "/selected", "/prescreened", "/queue"):
            return default
        query = {k: v for k, v in parse_qs(parsed.query).items() if k in FILTER_KEYS + ("role", "page")}
        encoded = urlencode(query, doseq=True)
        return parsed.path + ("?" + encoded if encoded else "")
    except ValueError:
        return default
