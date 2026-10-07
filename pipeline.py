from __future__ import annotations

if __name__ == "__main__":
    from jp.cli import main
    raise SystemExit(main())
else:
    # 保留原核心测试的 import/monkeypatch 接口，公开入口为 job-pipeline。
    import sys
    from jp import legacy
    sys.modules[__name__] = legacy
