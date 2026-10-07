from __future__ import annotations
import json
import pathlib
import re
from typing import Any, Dict, List

import jieba
import yaml

jieba.setLogLevel(60)  # 静音

STOP = set("的 了 与 和 及 或 在 为 对 基于 使用 通过 进行 负责 参与 完成 实现 支持 相关 等 一个 以及 the a an and or of to in for with on by is are be as at from".split())
_EN = re.compile(r"[A-Za-z][A-Za-z0-9+#._-]{1,}")


def tokenize(text: str) -> List[str]:
    text = text or ""
    out: List[str] = []
    for m in _EN.finditer(text):
        out.append(m.group(0).lower().strip("._-"))
    zh = re.sub(r"[A-Za-z0-9+#._/()（）:：,，、;；\s-]+", " ", text)
    for w in jieba.cut(zh):
        w = w.strip()
        if len(w) >= 2 and w not in STOP:
            out.append(w)
    seen, uniq = set(), []
    for t in out:
        if t and t not in STOP and t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def _load(facts_dir: pathlib.Path, name: str) -> Any:
    p = facts_dir / name
    if not p.exists():
        return None
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _entry(id_: str, type_: str, title: str, text: str, grade: Any = "") -> Dict[str, Any]:
    return {"id": id_, "type": type_, "title": title, "text": text,
            "keywords": tokenize(title + " " + text), "grade": str(grade or "")}


def build(facts_dir) -> List[Dict[str, Any]]:
    facts_dir = pathlib.Path(facts_dir)
    entries: List[Dict[str, Any]] = []

    skills = _load(facts_dir, "skills.yaml") or {}
    for cat, val in skills.items():
        if cat == "evidence" or not isinstance(val, str):
            continue
        entries.append(_entry("skill.%s" % cat, "skill", cat, val))

    for fname, type_ in (("projects.yaml", "project"), ("experiences.yaml", "experience"), ("research.yaml", "research")):
        for item in _load(facts_dir, fname) or []:
            title = item.get("name") or item.get("title") or item.get("org") or item.get("id")
            head = " ".join(str(item.get(k, "")) for k in ("role", "period", "dept", "venue") if item.get(k))
            for f in item.get("facts", []) or []:
                entries.append(_entry(f["id"], type_, title, f.get("claim", ""), f.get("grade", item.get("grade", ""))))
            sub_items = item.get("items")
            if isinstance(sub_items, list):
                for idx, sub in enumerate(sub_items, 1):
                    if not isinstance(sub, dict):
                        continue
                    sub_title = sub.get("title") or sub.get("name") or item["id"]
                    parts = []
                    for k in ("role", "venue", "arxiv", "note", "contribution"):
                        v = sub.get(k)
                        if isinstance(v, str) and v.strip():
                            parts.append("arXiv %s" % v.strip() if k == "arxiv" else v.strip())
                    entries.append(_entry("%s.%d" % (item["id"], idx), type_, sub_title, " ".join(parts),
                                           sub.get("grade", item.get("grade", ""))))
            if not item.get("facts") and not isinstance(sub_items, list):
                entries.append(_entry(item["id"], type_, title, head + " " + str(item.get("contribution", "")), item.get("grade", "")))

    for item in _load(facts_dir, "education.yaml") or []:
        courses = item.get("courses") or []
        course_names = [c["name"] if isinstance(c, dict) else str(c) for c in courses]
        text = "%s %s %s" % (item.get("degree", ""), item.get("status", ""), " ".join(course_names))
        entries.append(_entry(item["id"], "education", item.get("school", ""), text, item.get("grade", "")))

    for item in _load(facts_dir, "awards.yaml") or []:
        entries.append(_entry("award.%s.%s" % (item.get("year", ""), item.get("name", "")), "award", item.get("name", ""), str(item.get("year", ""))))

    profile = _load(facts_dir, "profile.yaml") or {}
    if profile.get("languages"):
        entries.append(_entry("language", "language", "语言", str(profile["languages"])))
    return entries


def write(entries: List[Dict[str, Any]], path) -> None:
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=1)


def load(path) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
