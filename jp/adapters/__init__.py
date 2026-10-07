from __future__ import annotations
from typing import Dict, List

REGION_SOURCES: Dict[str, List[str]] = {
    "CN": ["boss", "nowcoder", "watchlist"],
    "HK": ["linkedin", "jobsdb", "watchlist"],
}
BROWSER_SOURCES = {"boss", "nowcoder", "linkedin"}   # 需要 Edge 开着且 OpenCLI 扩展已连接


def get_adapter(source: str):
    """按来源名取适配器实例（惰性导入，避免循环依赖）。watchlist 不在这里（走 jp.adapters.watchlist.run_watchlist）。"""
    if source == "boss":
        from jp.adapters.boss import BossAdapter
        return BossAdapter()
    if source == "nowcoder":
        from jp.adapters.nowcoder import NowcoderAdapter
        return NowcoderAdapter()
    if source == "linkedin":
        from jp.adapters.linkedin import LinkedInAdapter
        return LinkedInAdapter()
    if source == "jobsdb":
        from jp.adapters.jobsdb import JobsdbAdapter
        return JobsdbAdapter()
    raise KeyError("未知来源: %s" % source)
