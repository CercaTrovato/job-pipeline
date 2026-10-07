from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class Status:
    FETCHED = "fetched"
    PRESCREENED_OUT = "prescreened_out"
    REJECTED_HARD = "rejected_hard"
    QUEUED = "queued"
    ANALYZED = "analyzed"
    MATCHED = "matched"
    PENDING_REVIEW = "pending_review"
    SKIPPED = "skipped"
    DUPLICATE = "duplicate"
    LATER = "later"
    APPROVED = "approved"
    NEEDS_VARIANT = "needs_variant"
    ADAPTER_BROKEN = "adapter_broken"
    RESUME_READY = "resume_ready"
    FORM_FILLED = "form_filled"
    SUBMITTED = "submitted"

    ALL = {
        FETCHED, PRESCREENED_OUT, REJECTED_HARD, QUEUED, ANALYZED, MATCHED,
        PENDING_REVIEW, SKIPPED, DUPLICATE, LATER, APPROVED, NEEDS_VARIANT, ADAPTER_BROKEN,
        RESUME_READY, FORM_FILLED, SUBMITTED,
    }


@dataclass
class RawJob:
    """适配器 / agent 手浏览 / 内推入口统一输出。必填三项（title / company / url），其余可空。"""
    platform_id: str
    title: str
    company: str
    url: str
    jd_text: str = ""
    location: str = ""
    salary_raw: str = ""
    posted_at: str = ""
    jd_lang: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RawJob":
        missing = [k for k in ("title", "company", "url") if not d.get(k)]
        if missing:
            raise ValueError("RawJob 缺少必填字段: %s" % ", ".join(missing))
        known = {k: d.get(k, "") for k in ("platform_id", "title", "company", "url", "jd_text",
                                            "location", "salary_raw", "posted_at", "jd_lang")}
        return cls(raw=d.get("raw", {}), **known)
