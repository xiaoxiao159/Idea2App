"""Idea2App worker:每项目一个子进程,supervisor 用 `-m backend.worker` 拉起。

supervisor 通过环境变量注入全局 API 配置(见 registry.to_env),通过命令行参数传
--project-dir / --port。本模块顶部**不得** import agent / Idea2App / backend(否则
WORKDIR 快照落在 supervisor 的 cwd 而非项目目录),全部放在 main() chdir 之后延迟导入。

IDEA2APP_FAKE_MODEL=1 时把 get_model 换成假模型(测试用,不打真 API)。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _fake_model():
    from agent.models.base import Model

    class Fake(Model):
        def complete(self, messages, system, tools, max_tokens=8000):
            return {"content": [{"type": "text", "text": "fake ok"}],
                    "stop_reason": "end_turn", "usage": {"input": 1, "output": 1}}

    return Fake()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Idea2App worker(每项目一个子进程)")
    ap.add_argument("--project-dir", required=True)
    ap.add_argument("--port", type=int, required=True)
    args = ap.parse_args(argv)

    project_dir = Path(args.project_dir).resolve()
    # WORKDIR 铁律:先 chdir 再 import agent(team.py 在 import 时快照 Path.cwd())。
    os.chdir(project_dir)

    _ROOT = Path(__file__).resolve().parent.parent  # code_agent
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))

    from agent.config import build_config  # noqa: E402
    from backend.project_server import ProjectServer  # noqa: E402

    config = build_config(os.environ)  # 直接造,不调 load_config(避开 .env 覆盖注入值)

    if os.environ.get("IDEA2APP_FAKE_MODEL") == "1":
        from Idea2App import bootstrap as _bootstrap
        _bootstrap.get_model = lambda c: _fake_model()

    server = ProjectServer(config=config, port=args.port)
    server.run()


if __name__ == "__main__":
    main()