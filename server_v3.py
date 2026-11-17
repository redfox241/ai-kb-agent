"""兼容入口（8002 人工测试）。

实现已统一到 server.py，本文件只保留启动入口，避免三份代码分叉。
端口由环境变量 RAG_PORT 控制（默认 8002）。
"""
import os

os.environ.setdefault("RAG_PORT", os.environ.get("RAG_V3_PORT", "8002"))
os.environ.setdefault("RAG_PROFILE", "test")

from server import run_server

if __name__ == "__main__":
    run_server()
