from __future__ import annotations

import argparse
import json
import pathlib
import sys
import webbrowser

import yaml
from filelock import Timeout

from jp import settings
from jp.security import safe_error


def parser():
    p = argparse.ArgumentParser(prog="job-pipeline", description="本地求职工作台")
    p.add_argument("--workspace", help="独立用户工作区；也可设置 JP_WORKSPACE")
    sub = p.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="打开本地看板")
    start.add_argument("--port", type=int, default=5050)
    start.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    sub.add_parser("demo", help="导入离线虚构演示岗位")
    d = sub.add_parser("doctor", help="脱敏诊断环境与模型")
    d.add_argument("--json", action="store_true")
    d.add_argument("--test-model", action="store_true", help="一次虚构内容的最小调用，可能消耗额度")
    c = sub.add_parser("configure", help="原子保存候选配置")
    c.add_argument("--from", dest="from_file", required=True)
    c.add_argument("--test-model", action="store_true", help="保存前测试模型，失败保留原配置")
    sub.add_parser("agent-prompt", help="输出可复制的 Agent 配置提示词")
    facts = sub.add_parser("import-facts", help="可选：从 resume-workflow/facts YAML 目录导入事实")
    facts.add_argument("--from", dest="from_dir", required=True)
    for name in ("fetch", "run"):
        command = sub.add_parser(name, help="采集" if name == "fetch" else "分析匹配，可先采集")
        command.add_argument("--region", choices=["CN", "HK"], default="CN")
        command.add_argument("--source", action="append")
        command.add_argument("--fetch", action="store_true")
        command.add_argument("--max-jobs", type=int)
        command.add_argument("--job-id", action="append")
    old = sub.add_parser("legacy", help="现有高级流水线命令，使用当前工作区配置")
    old.add_argument("arguments", nargs=argparse.REMAINDER)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        workspace = settings.Workspace(args.workspace).initialize()
        if args.command == "agent-prompt":
            print(settings.agent_prompt(workspace))
            return 0
        if args.command == "doctor":
            from jp.diagnostics import check
            result = check(workspace.runtime_config(), args.test_model)
            print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else result["message"] + "\n" + result.get("next_step", ""))
            return 0 if result["status"] in ("configured", "manual") else 1
        from jp.service import Service
        service = Service(workspace)
        if args.command == "configure":
            from jp.diagnostics import check
            candidate = yaml.safe_load(pathlib.Path(args.from_file).read_text(encoding="utf-8"))

            def tester(cfg):
                result = check(cfg, True)
                if result["status"] not in ("configured", "manual"):
                    raise ValueError(result["message"])
                return result

            with service.lock:
                result = settings.configure(workspace, candidate, tester if args.test_model else None)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "demo":
            with service.lock:
                print(json.dumps(service.demo(), ensure_ascii=False))
        elif args.command == "import-facts":
            from jp import facts_index, resume
            with service.lock:
                entries = facts_index.build(pathlib.Path(args.from_dir).resolve())
                if not entries:
                    raise ValueError("该目录没有可导入的事实，请选择含技能、项目或经历 YAML 的目录。")
                for entry in entries:
                    for key in ("title", "text"):
                        entry[key] = resume.redact(entry[key])
                    entry["keywords"] = facts_index.tokenize(entry["title"] + " " + entry["text"])
                from jp.storage import atomic_text
                path = pathlib.Path(service.config()["paths"]["facts_index"])
                path.with_suffix(".json.bak").write_bytes(path.read_bytes())
                atomic_text(path, json.dumps(entries, ensure_ascii=False, indent=2))
                print("已导入 %d 条自述事实；未验证经历真实性，未调用模型。" % len(entries))
        elif args.command == "start":
            if not 1 <= args.port <= 65535:
                raise ValueError("端口必须为 1–65535。")
            from board.app import create_app
            from jp.service import RESOURCES
            from waitress import create_server
            app = create_app(workspace.runtime_config()["paths"]["db"], RESOURCES / "schema.sql", service=service)
            url = "http://127.0.0.1:%d" % args.port
            try:
                server = create_server(app, host="127.0.0.1", port=args.port)
            except OSError:
                raise ValueError("该端口无法监听，请用 --port 指定其他可用端口。")
            print("看板：" + url + "\n工作区：" + str(workspace.root), flush=True)
            if not args.no_open:
                import threading
                threading.Timer(1, lambda: webbrowser.open(url + "/setup")).start()
            server.run()
        elif args.command in ("fetch", "run"):
            from jp.tasks import TaskRunner
            # 同步 CLI 和网页使用相同的锁与业务服务。
            TaskRunner(service)
            options = {"region": args.region}
            if args.source: options["sources"] = args.source
            if args.max_jobs: options["max_jobs"] = args.max_jobs
            if args.job_id: options["job_ids"] = args.job_id
            kind = "fetch" if args.command == "fetch" else "pipeline" if args.fetch else "analyze"
            with service.lock:
                print(json.dumps(service.run(kind, options, stage=lambda name, _: print(name)), ensure_ascii=False))
        elif args.command == "legacy":
            from jp import legacy
            parsed = legacy.build_parser().parse_args(args.arguments)
            with service.lock:
                parsed.fn(parsed, workspace.runtime_config())
        return 0
    except Timeout:
        print("已有任务正在运行，请在看板停止或等待完成。", file=sys.stderr)
        return 2
    except Exception as exc:
        print(safe_error(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
