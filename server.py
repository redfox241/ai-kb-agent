"""AI-KB 兼容入口：保留给 scripts/service.sh、server_auto.py、server_v3.py。

实际组合根与启动实现见 bootstrap.py。
"""
from bootstrap import PORT, answer_question, app, effective_prompt, run_server

__all__ = ["PORT", "answer_question", "app", "effective_prompt", "run_server"]

if __name__ == "__main__":
    run_server()
