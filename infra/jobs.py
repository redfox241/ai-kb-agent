"""进程内后台任务：当前用线程池，接口保留替换空间。"""
from __future__ import annotations

import itertools
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from domain.models import JobHandle


class ThreadJobRunner:
    def __init__(self, max_workers: int = 4) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max(1, int(max_workers)))
        self._counter = itertools.count(1)
        self._lock = threading.Lock()
        self._closed = False

    def start(self) -> None:
        return None

    def submit(self, name: str, fn: Callable[[], None]) -> JobHandle:
        with self._lock:
            if self._closed:
                raise RuntimeError("JobRunner 已关闭")
            job_id = f"job_{next(self._counter)}"
        self._executor.submit(fn)
        return JobHandle(id=job_id, name=name)

    def shutdown(self, wait: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=wait)
