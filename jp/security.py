from __future__ import annotations

import os
import re


def safe_error(exc):
    message = str(exc)
    for name, value in os.environ.items():
        if value and len(value) >= 8 and any(word in name.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            message = message.replace(value, "[已隐藏]")
    message = re.sub(r"sk-[A-Za-z0-9_-]+|Bearer\s+\S+", "[已隐藏]", message, flags=re.I)
    # 网络及模型异常可能含响应正文，公开错误只报告类型。
    if not isinstance(exc, (ValueError, RuntimeError, FileNotFoundError)):
        return type(exc).__name__ + "：操作失败，请检查设置和连接。"
    return message[:400]
