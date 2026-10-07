from __future__ import annotations
import json

from jp import db as jpdb, decide, dedup, ingest
from jp.models import RawJob, Status

JD = ("岗位职责：1，参与制定公司级 AI 技术发展规划，明确重点方向与实施路径。"
      "2，主导设计并构建企业级 AI Agent 智能体平台，支持智能应用的快速开发与部署。"
      "3，负责与业务沟通，识别 AI 应用落地场景并研究可行性方案。"
      "任职要求：计算机、人工智能等相关专业本科及以上学历；熟悉 Python 与常见大模型框架；每周到岗 4 天以上。")


JD2 = ("Project Assistant: support the regional project team on schedule tracking, vendor coordination and"
       " reporting; prepare weekly status decks and maintain the issue log. Requirements: bachelor degree in"
       " progress, fluent English and Mandarin, proficient with Excel and PowerPoint, able to work onsite.")


def seed(conn, company, title, jd=JD, status=Status.FETCHED, prescore=50.0, region="CN", location="深圳"):
    job_id = ingest.ingest_jobs(conn, [RawJob(platform_id=company + title, title=title, company=company,
                                              url="https://example.org/" + title, jd_text=jd, location=location)],
                                "nowcoder", region).job_ids[0]
    conn.execute("UPDATE jobs SET prescore=? WHERE job_id=?", (prescore, job_id))
    jpdb.set_status(conn, job_id, status)
    return job_id


def raw_of(conn, job_id):
    return json.loads(conn.execute("SELECT raw_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0] or "{}")


def status_of(conn, job_id):
    return conn.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]


def test_same_employer_recognises_subsidiaries_and_rejects_strangers():
    assert dedup.same_employer("华为HUAWEI", "上海华为技术有限公司")
    assert dedup.same_employer("成都华为技术有限公司", "华为软件技术有限公司")
    assert dedup.same_employer("Binance", "Binance")
    assert dedup.same_employer("深圳市腾讯计算机系统有限公司", "腾讯科技")
    assert not dedup.same_employer("上海得物信息集团有限公司", "网易")
    assert not dedup.same_employer("广州正道科技有限公司", "洽客科技")        # 只共用"科技"不算同一家
    assert not dedup.same_employer("深圳思达资讯科技有限公司", "4399游戏")


def test_multi_employer_cluster_keeps_one_and_flags_it(conn):
    """同一份 JD 挂在 4 家互不相关的公司名下 = 中介刷帖：留分最高的一条并标注，其余转 duplicate。"""
    keep = seed(conn, "网易", "大模型算法工程师", prescore=80.0)
    others = [seed(conn, c, "大模型算法", prescore=40.0) for c in ("上海得物信息集团有限公司", "用友", "4399游戏")]
    res = dedup.run(conn)
    assert (res.clusters, res.spam, res.duplicates, res.flagged) == (1, 3, 0, 1)
    assert status_of(conn, keep) == Status.FETCHED
    assert [status_of(conn, j) for j in others] == [Status.DUPLICATE] * 3
    raw = raw_of(conn, keep)
    assert raw["dup_cluster"] == {"kind": "multi_employer", "employers": 4, "size": 4}
    assert raw["soft_flags"] == ["疑似刷帖：同一 JD 挂在 4 家公司名下"]
    assert raw_of(conn, others[0])["dup_of"] == keep
    assert "刷帖" in raw_of(conn, others[0])["dup_reason"]


def test_same_employer_cluster_folds_without_spam_flag(conn):
    """同一雇主不同主体的重复帖：折叠但不标刷帖，代表帖取状态最靠后的那条（已分析的不白跑）。"""
    pending = seed(conn, "上海华为技术有限公司", "AI应用工程师", status=Status.PENDING_REVIEW, prescore=10.0)
    fetched = seed(conn, "华为HUAWEI", "AI应用工程师", prescore=90.0)
    res = dedup.run(conn)
    assert (res.clusters, res.duplicates, res.spam, res.flagged) == (1, 1, 0, 0)
    assert status_of(conn, pending) == Status.PENDING_REVIEW and status_of(conn, fetched) == Status.DUPLICATE
    assert "dup_cluster" not in raw_of(conn, pending) and raw_of(conn, pending).get("soft_flags") is None


def test_same_employer_in_different_cities_is_kept_separate(conn):
    """同一雇主同一份 JD 分城市开的岗位是不同岗位：按城市分开折叠；没写地点的退回按标题分组。"""
    sz = [seed(conn, "华为HUAWEI", "AI应用工程师"), seed(conn, "成都华为技术有限公司", "AI应用工程师", location="成都"),
          seed(conn, "华为软件技术有限公司", "AI应用工程师")]
    blank = [seed(conn, "海柔创新", "项目助理岗（%s，中国）" % city, jd=JD2, location="") for city in ("上海", "深圳")]
    res = dedup.run(conn)
    assert (res.clusters, res.duplicates) == (1, 1)                 # 只有深圳那两条合并
    assert status_of(conn, sz[1]) == Status.FETCHED                 # 成都的那条留着
    assert [status_of(conn, j) for j in blank] == [Status.FETCHED] * 2
    assert sorted(status_of(conn, j) for j in (sz[0], sz[2])) == [Status.DUPLICATE, Status.FETCHED]


def test_multi_employer_template_does_not_hide_different_roles_or_cities(conn):
    """三家公司套同一 JD 模板不代表不同职能、不同城市是同一岗位。"""
    ids = [
        seed(conn, "网易", "算法实习生", prescore=90),
        seed(conn, "用友", "前端实习生", location="上海"),
        seed(conn, "4399游戏", "产品运营实习生", location="成都"),
    ]
    assert dedup.run(conn) == dedup.DedupResult()
    assert [status_of(conn, job_id) for job_id in ids] == [Status.FETCHED] * 3


def test_multi_employer_template_does_not_hide_different_roles_same_city(conn):
    ids = [seed(conn, "网易", "算法实习生"), seed(conn, "用友", "前端实习生"),
           seed(conn, "4399游戏", "产品运营实习生")]
    assert dedup.run(conn) == dedup.DedupResult()
    assert [status_of(conn, job_id) for job_id in ids] == [Status.FETCHED] * 3


def test_multi_employer_template_does_not_hide_same_role_in_different_cities(conn):
    ids = [seed(conn, "网易", "算法实习生"),
           seed(conn, "用友", "算法实习生", location="上海"),
           seed(conn, "4399游戏", "算法实习生", location="成都")]
    assert dedup.run(conn) == dedup.DedupResult()
    assert [status_of(conn, job_id) for job_id in ids] == [Status.FETCHED] * 3


def test_missing_location_marketing_suffix_still_respects_skipped_job(conn):
    """地点为空时，“急招”不应让已跳过的同岗重新出现在队列。"""
    skipped = seed(conn, "华为HUAWEI", "数据分析实习生", status=Status.SKIPPED, location="")
    repost = seed(conn, "华为HUAWEI", "数据分析实习生（急招）", location="")
    res = dedup.run(conn)
    assert (res.clusters, res.duplicates) == (1, 1)
    assert status_of(conn, skipped) == Status.SKIPPED
    assert status_of(conn, repost) == Status.DUPLICATE
    assert raw_of(conn, repost)["dup_of"] == skipped


def test_two_unrelated_companies_and_short_jd_are_left_alone(conn):
    """两家同 JD 多半是同岗跨平台；过短的 JD 判不了重——都不动。"""
    a = seed(conn, "Everest Global", "Intern, UX/UI")
    b = seed(conn, "EV Technologies Limited", "Internship, UX/UI")
    c = seed(conn, "甲公司", "实习生", jd="岗位职责：详见官网。")
    d = seed(conn, "乙公司", "实习生", jd="岗位职责：详见官网。")
    assert dedup.run(conn) == dedup.DedupResult()
    assert [status_of(conn, j) for j in (a, b, c, d)] == [Status.FETCHED] * 4


def test_decided_jobs_are_never_folded_and_run_is_idempotent(conn):
    """人工已裁决（投 / 放弃）的不动；重复跑不会重复计数，也不会换代表帖。"""
    approved = seed(conn, "网易", "大模型算法", status=Status.APPROVED)
    skipped = seed(conn, "用友", "大模型算法", status=Status.SKIPPED)
    live = seed(conn, "4399游戏", "大模型算法")
    first = dedup.run(conn)
    assert (first.spam, first.flagged) == (1, 0)          # 代表帖是 approved，不在可改状态里，不加标注
    assert status_of(conn, approved) == Status.APPROVED and status_of(conn, skipped) == Status.SKIPPED
    assert status_of(conn, live) == Status.DUPLICATE
    assert dedup.run(conn) == dedup.DedupResult()


def test_requeued_duplicate_is_not_folded_again(conn):
    """在看板上把一条 duplicate 重新入队后，dedup 不再把它折叠回去。"""
    keep = seed(conn, "网易", "大模型算法工程师", prescore=80.0)
    others = [seed(conn, c, "大模型算法", prescore=40.0) for c in ("用友", "4399游戏", "上海得物信息集团有限公司")]
    dedup.run(conn)
    decide.requeue(conn, others[0])
    assert status_of(conn, others[0]) == Status.QUEUED
    raw = raw_of(conn, others[0])
    assert raw["dup_keep"] is True and "dup_of" not in raw
    dedup.run(conn)
    assert status_of(conn, others[0]) == Status.QUEUED
    assert status_of(conn, keep) == Status.FETCHED


def test_near_duplicate_does_not_resurface_after_user_skipped_same_role(conn):
    """同岗 JD 仅微调措辞时，已跳过的帖可作为代表，避免新帖再次进入待挑选。"""
    original = seed(conn, "华为云计算技术有限公司", "算法工程师", status=Status.SKIPPED)
    changed = seed(conn, "华为云", "算法工程师", jd=JD.replace("识别 AI 应用落地场景", "识别 AI 技术应用落地场景"))
    res = dedup.run(conn)
    assert (res.clusters, res.duplicates) == (1, 1)
    assert status_of(conn, original) == Status.SKIPPED
    assert status_of(conn, changed) == Status.DUPLICATE
    assert raw_of(conn, changed)["dup_of"] == original
    assert "近似" in raw_of(conn, changed)["dup_reason"]
    assert dedup.run(conn) == dedup.DedupResult()


def test_near_duplicate_with_marketing_suffix_does_not_resurface(conn):
    original = seed(conn, "华为HUAWEI", "数据分析实习生", status=Status.SKIPPED, location="")
    repost = seed(conn, "华为HUAWEI", "数据分析实习生（急招）", location="",
                  jd=JD.replace("识别 AI 应用落地场景", "识别 AI 技术应用落地场景"))
    assert dedup.run(conn).duplicates == 1
    assert status_of(conn, original) == Status.SKIPPED
    assert status_of(conn, repost) == Status.DUPLICATE


def test_similar_text_keeps_distinct_track_and_hard_requirements(conn):
    """实习/校招及每周天数、最短月数变化会影响申请决定，不能因高相似度而折叠。"""
    base = JD.replace("每周到岗 4 天以上", "每周到岗 5 天，至少 3 个月")
    ids = [
        seed(conn, "华为HUAWEI", "AI应用工程师（实习）", jd=base),
        seed(conn, "华为软件技术有限公司", "AI应用工程师（实习）", jd=base.replace("5 天", "7 天")),
        seed(conn, "上海华为技术有限公司", "AI应用工程师（实习）", jd=base.replace("3 个月", "6 个月")),
        seed(conn, "华为云", "AI应用工程师（校招）", jd=base),
    ]
    assert dedup.run(conn) == dedup.DedupResult()
    assert [status_of(conn, job_id) for job_id in ids] == [Status.FETCHED] * 4


def test_exact_template_is_not_enough_to_merge_different_roles(conn):
    ai = seed(conn, "华为HUAWEI", "AI应用工程师")
    java = seed(conn, "华为软件技术有限公司", "Java开发工程师")
    assert dedup.run(conn) == dedup.DedupResult()
    assert status_of(conn, ai) == status_of(conn, java) == Status.FETCHED


def test_same_jd_full_time_and_part_time_are_separate_jobs(conn):
    full = seed(conn, "ALO", "Operations Associate (Full-Time)", jd=JD2, region="HK", location="Hong Kong")
    part = seed(conn, "ALO", "Operations Associate (Part-Time)", jd=JD2, region="HK", location="Hong Kong")
    assert dedup.run(conn) == dedup.DedupResult()
    assert status_of(conn, full) == status_of(conn, part) == Status.FETCHED


def test_exact_repost_after_skip_stays_out_of_review(conn):
    skipped = seed(conn, "华为HUAWEI", "AI应用工程师", status=Status.SKIPPED)
    repost = seed(conn, "华为软件技术有限公司", "AI应用工程师")
    assert dedup.run(conn).duplicates == 1
    assert status_of(conn, skipped) == Status.SKIPPED
    assert status_of(conn, repost) == Status.DUPLICATE


def test_near_fold_redirects_exact_duplicate_children(conn):
    a = seed(conn, "华为HUAWEI", "算法工程师", prescore=80.0)
    child = seed(conn, "华为软件技术有限公司", "算法工程师", prescore=40.0)
    approved = seed(conn, "华为云", "算法工程师",
                    jd=JD.replace("识别 AI 应用落地场景", "识别 AI 技术应用落地场景"),
                    status=Status.APPROVED)
    res = dedup.run(conn)
    assert (res.clusters, res.duplicates) == (2, 2)
    assert status_of(conn, approved) == Status.APPROVED
    assert status_of(conn, a) == status_of(conn, child) == Status.DUPLICATE
    assert raw_of(conn, a)["dup_of"] == approved
    assert raw_of(conn, child)["dup_of"] == approved
