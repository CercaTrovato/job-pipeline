from __future__ import annotations
import json
import pathlib
import re
from typing import Any, Dict

_FM = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)

TAIL = """

---

## 输入

下面是本任务包的 `input.json`（与同目录文件完全一致）：

```json
{input_json}
```

## 输出要求

- 把结果写到本目录的 `output.json`，且必须通过同目录 `schema.json` 的校验。
- 只写 JSON，不要 Markdown 围栏，不要解释文字。
- 无法完成时不要写 `output.json`，改写 `error.txt` 说明原因。
"""


def render(step: str, input_obj: Dict[str, Any], specs_dir) -> str:
    spec_path = pathlib.Path(specs_dir) / step / "SKILL.md"
    if not spec_path.exists():
        raise FileNotFoundError(str(spec_path))
    body = _FM.sub("", spec_path.read_text(encoding="utf-8"), count=1)
    return body.rstrip() + TAIL.format(input_json=json.dumps(input_obj, ensure_ascii=False, indent=1))


def for_codex(prompt_text: str) -> str:
    """CLI 通过 --output-schema/-o 写输出；去掉给手动填包者的文件操作指令。"""
    return prompt_text.rsplit("\n## 输出要求\n", 1)[0]
