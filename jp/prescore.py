from __future__ import annotations
import json
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from jp import browse
from jp import db as jpdb
from jp import dedup
from jp import facts_index as fi
from jp.models import Status
from jp.rules import hard


@dataclass
class PrescoreResult:
    queued: int = 0
    out: int = 0        # 关键词命中不足 min_overlap → prescreened_out
    killed: int = 0     # 规则判硬条件不合 → rejected_hard
    backlog: int = 0    # 相关但排在 top_n 之外 → 留在 fetched，下次 prescore 继续出队


SCALE = 50.0                       # 加权命中和 ≈ 50 → 63 分，≈ 115 → 90 分（按全库分布校准：中位数 ≈ 47，p95 ≈ 119）
# 事实索引是从简历事实里切词得到的，混着不少"项目细节 / 语境词"（干部、台账、问卷、阈值、雅思……）。它们在 JD 里稀有，
# IDF 反而高，会把无关岗位顶上去。粗筛时把这些非技能词剔掉（只影响粗筛，不改索引与画像哈希）。
PRESCORE_STOP = set("""
三层 主线 候选 公共 加权 口碑 可信度 合并 噪音 复核 学士 学院 封顶 干部 广度 引流 强制 归类 打分 排斥 术语 消融 登记 科技部
粤语 纠错 红线 自述 覆盖率 证据 门禁 阈值 不确定性 严格 关键词 回溯 大学 截止 断点 普通话 替代 最小 科研成果 线索 解释 评论
话术 雅思 入库 兼容 冲突 反作弊 投稿 既有 映射 本地 校准 研一 私有 程序 第三方 缺失 触发 评价 课堂 资讯 问卷 高保真 一步
交接 伦理 修正 六大 分工 回答 官方 室内 成都 指南 横向 翻译 银行 雷达 不足 云端 仓库 作者 写作 前置 后台 微信 提交 比较
活跃度 浏览器 窗口 评分 链接 阿里 付费 会计 台账 失效 官网 来源 缺陷 网页 视图 错误 限流 人工 公开 分发 审查 小红书 意图
排序 监管 解读 为主 代理 依赖 失败 已有 损失 样本 过滤 优势 依据 全部 决定 准确率 增量 对象 数据表 权重 校验 留学 重复
隔离 两个 保底 地区 学校 提取 源码 结论 输入 长程 院校 修复 参考 图片 恢复 提炼 社媒 证明 路由 页面 文件 标签 清单 用于
申请 联合 重构 高水平 学生 对抗 对比 维度 可能 成功 服务器 验收 典型 匹配 管线 邮件 分享 类型 培养 实验室 审计 形成 最终
期间 规则 配置 全局 引入 成员 成绩 接入 日志 监督 审核 敏感 机构 速度 业务流程 原型 概念 课程 一致性 协议 延迟 整合 权限
研究生 记忆 材料 案例 第一 第二 统一 解析 资料 只是 节点 计划 升级 组件 领先 积累 建议 记录 金融 判断 制作 追踪 筛选 适应
版本 瓶颈 风险 承担 拆解 系列 边界 条件 科学 方式 产出 安全 操作 竞赛 报告 商业 同类 实验 员工 科技 自动 部门 时间 运行
循环 发现 稳定 内部 客户 规范 可行性 信息 提示 完整 结果 关注 整理 指标 覆盖 驱动 交付 在读 真实 英语 实施 辅助 逻辑 目标
机制 硕士 模块 知识 理论 内容 质量 原理 过程 体系 针对 策略 面向 反馈 处理 推进 方法 决策 常用 迭代 验证 性能 输出 机器
探索 文档 服务 任务 核心 独立 分项 以上 协助 语言 快速 智能 江门 分行 分公司 中国工商银行 中国电信 指南针 studycompass
五门 示例 印本 份文件 三档 一等 奖学金 母语 香港城市大学 理学 编码 标准化 排序 引擎 数据表 解析 组件
acc docx fm ielts medium missing samples low sciences enhanced method methodology original class credit grade draft expert
pattern windows challenge challenging medical languages writing handling integration artificial high review code business
information university computer applications systems engineering intelligence learning data k+ pdf qs
""".split())
TECH_ROLES = {"ai", "data", "data-eng", "software", "backend"}
NONTECH_ROLES = {"product", "ops", "design", "hardware", "business", "corporate"}
TECH_BONUS, NONTECH_PENALTY = 8.0, 20.0


def idf_table(texts: List[str]) -> Dict[str, float]:
    """语料级逆文档频率 log((N+1)/(df+1))：一半 JD 都有的词（要求 / 能力 / 技术 / 工作）≈ 0.7，一成 JD 有的 ≈ 2.3，
    百分之一的（pytorch / rag / 聚类 / lightgbm）≈ 4.6——泛词再多也压不过几个真正对口的技能词。"""
    n = len(texts)
    df: Dict[str, int] = {}
    for t in texts:
        for tok in set(fi.tokenize(t)):
            df[tok] = df.get(tok, 0) + 1
    return {tok: math.log((n + 1.0) / (c + 1.0)) for tok, c in df.items()}


def score_text(jd_text: str, title: str, index_keywords: Set[str], idf: Optional[Dict[str, float]] = None) -> Tuple[float, List[str]]:
    """粗筛分 0–100：JD 与事实索引关键词的去重命中按 IDF 加权求和（标题命中再计一次），软饱和到 100；
    再按标题职能加减（产品 / 运营 / 设计 / 硬件 / 商务 / 人事类 −20，否则技术 / 数据类 +8）。不命中任何关键词 → 0。"""
    idf = idf or {}
    hits = [t for t in fi.tokenize(jd_text) if t in index_keywords]        # tokenize 已去重
    if not hits:
        return 0.0, []
    title_hits = [t for t in fi.tokenize(title) if t in index_keywords]
    raw = sum(idf.get(t, 1.0) for t in hits) + sum(idf.get(t, 1.0) for t in title_hits)
    score = 100.0 * (1.0 - math.exp(-raw / SCALE))
    roles = set(browse.title_tags(title)[0]) if title else set()
    if roles & NONTECH_ROLES:          # "硬件工程师 / Hardware Development Engineer" 同时命中硬件与软件：职能词优先降权
        score -= NONTECH_PENALTY
    elif roles & TECH_ROLES:
        score += TECH_BONUS
    hits.sort(key=lambda t: -idf.get(t, 1.0))      # 权重高的对口词排前面，便于看板 / 日志展示
    return round(max(0.0, min(100.0, score)), 1), hits


def run(conn, facts_entries: List[Dict[str, Any]], constraints: Dict[str, Any], top_n: int, min_overlap: int) -> PrescoreResult:
    kw = {k for e in facts_entries for k in e["keywords"]} - PRESCORE_STOP
    res = PrescoreResult()
    rows = jpdb.jobs_by_status(conn, Status.FETCHED)
    if not rows:
        return res
    idf = idf_table([r[0] or "" for r in conn.execute("SELECT jd_text FROM jobs")])   # 全库语料，含历史岗位
    already = conn.execute("SELECT COUNT(*) FROM jobs WHERE status=?", (Status.QUEUED,)).fetchone()[0]
    capacity = max(0, top_n - already)      # top_n 是队列容量：run 先粗筛再出队时只补齐，不额外再出一批
    scored = []
    for row in rows:
        # 花接口调用之前先用规则判：JD 文本正则 + 抓取器给的地点 / 到岗天数 / 招聘类型；轨道（实习 / 校招）与软标注记进 raw_json
        hr = hard.check_job(row, constraints)
        raw = json.loads(row["raw_json"] or "{}")
        raw["track"], raw["soft_flags"] = hr.track, hr.flags + dedup.cluster_flags(raw)
        if not hr.passed:
            raw["prescreen_reasons"] = hr.reasons
            conn.execute("UPDATE jobs SET raw_json=?, prescore=0 WHERE job_id=?", (jpdb.json_dumps(raw), row["job_id"]))
            jpdb.set_status(conn, row["job_id"], Status.REJECTED_HARD)
            res.killed += 1
            continue
        s, hits = score_text(row["jd_text"], row["title"] or "", kw, idf)
        raw["prescore_hits"] = hits[:30]
        conn.execute("UPDATE jobs SET raw_json=?, prescore=? WHERE job_id=?", (jpdb.json_dumps(raw), s, row["job_id"]))
        if len(hits) < min_overlap:
            jpdb.set_status(conn, row["job_id"], Status.PRESCREENED_OUT)
            res.out += 1
            continue
        scored.append((s, row["job_id"], row["region"]))
    for rank, (s, job_id, _) in enumerate(interleave_by_region(scored)):
        if rank < capacity:
            jpdb.set_status(conn, job_id, Status.QUEUED)
            res.queued += 1
        else:
            res.backlog += 1          # 保持 fetched
    conn.commit()
    return res


def interleave_by_region(scored: List[Tuple[float, str, str]]) -> List[Tuple[float, str, str]]:
    """各地区内部按分数排序，再按地区轮流取（CN、HK、CN、HK…）：英文 JD 对中文为主的事实索引命中天然偏少，
    只按全局分数取 top_n 会让 HK 岗位长期排不进队列。某地区取完后剩余名额归其它地区。"""
    by_region: Dict[str, List[Tuple[float, str, str]]] = {}
    for item in scored:
        by_region.setdefault(item[2], []).append(item)
    for lst in by_region.values():
        lst.sort(key=lambda x: (-x[0], x[1]))
    order = sorted(by_region)          # 固定地区顺序（CN 在前），保证幂等
    out: List[Tuple[float, str, str]] = []
    idx = {r: 0 for r in order}
    while len(out) < len(scored):
        for r in order:
            if idx[r] < len(by_region[r]):
                out.append(by_region[r][idx[r]])
                idx[r] += 1
    return out
