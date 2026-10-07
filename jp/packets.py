from __future__ import annotations
import hashlib
import json
import pathlib
import shutil
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import jsonschema

STEPS = ("profile", "analyze", "match", "resume", "variant_pick", "intro")


class PacketError(Exception):
    pass


@dataclass
class Packet:
    job_id: str
    step: str
    dir: pathlib.Path
    input_hash: str


def input_hash(obj: Any) -> str:
    canon = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _read_json(p: pathlib.Path) -> Any:
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(p: pathlib.Path, obj: Any) -> None:
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def create(tasks_dir, job_id: str, step: str, input_obj: Dict[str, Any], prompt_text: str, schema_path) -> Packet:
    if step not in STEPS:
        raise ValueError("未知 step: %s" % step)
    d = pathlib.Path(tasks_dir) / job_id / step
    h = input_hash(input_obj)
    if d.exists():
        old = d / "input.json"
        if old.exists() and input_hash(_read_json(old)) == h:
            return Packet(job_id, step, d, h)   # 幂等：输入未变，保留现有 output
        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)
    _write_json(d / "input.json", input_obj)
    (d / "prompt.md").write_text(prompt_text, encoding="utf-8")
    shutil.copyfile(str(schema_path), str(d / "schema.json"))
    return Packet(job_id, step, d, h)


def status(p: Packet) -> str:
    """done = 有 output.json 待校验/已校验；error = 上次校验失败且 agent 尚未重写 output；pending = 未填。
    output.json 存在时优先返回 done，这样 agent 在 error 后重写输出即可被 collect 重新校验。"""
    if (p.dir / "output.json").exists():
        return "done"
    if (p.dir / "error.txt").exists():
        return "error"
    return "pending"


def validate(p: Packet) -> Dict[str, Any]:
    schema_path = p.dir / "schema.json"
    if not schema_path.exists():
        msg = "schema.json 缺失"
        (p.dir / "error.txt").write_text(msg, encoding="utf-8")
        raise PacketError(msg)
    out_path = p.dir / "output.json"
    if not out_path.exists():
        raise PacketError("output.json 缺失: %s" % p.dir)
    try:
        out = _read_json(out_path)
        jsonschema.validate(out, _read_json(schema_path))
    except (json.JSONDecodeError, jsonschema.ValidationError) as e:
        msg = "%s: %s" % (type(e).__name__, str(e).splitlines()[0][:300])
        (p.dir / "error.txt").write_text(msg, encoding="utf-8")
        out_path.unlink()
        raise PacketError(msg)
    err = p.dir / "error.txt"
    if err.exists():
        err.unlink()
    return out


def load(tasks_dir, job_id: str, step: str) -> Optional[Packet]:
    d = pathlib.Path(tasks_dir) / job_id / step
    if not (d / "input.json").exists():
        return None
    return Packet(job_id, step, d, input_hash(_read_json(d / "input.json")))


def list_pending(tasks_dir, step: Optional[str] = None) -> List[Packet]:
    root = pathlib.Path(tasks_dir)
    if not root.exists():
        return []
    out: List[Packet] = []
    for job_dir in sorted(root.iterdir()):
        if not job_dir.is_dir():
            continue
        for step_dir in sorted(job_dir.iterdir()):
            if step and step_dir.name != step:
                continue
            p = load(root, job_dir.name, step_dir.name)
            if p and status(p) in ("pending", "error"):
                out.append(p)
    return out
