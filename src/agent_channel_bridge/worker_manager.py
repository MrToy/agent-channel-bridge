"""WorkerManager — manages lifecycle of all ACP workers."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .acp_worker import AcpWorker
from .config import config

log = logging.getLogger("onebot-bridge")


class WorkerManager:
    def __init__(self):
        self.workers: dict[str, AcpWorker] = {}
        self._on_reply_cb = None

    def add_worker(self, key: str, worker: AcpWorker):
        self.workers[key] = worker

    def get(self, key: str) -> Optional[AcpWorker]:
        return self.workers.get(key)

    def is_ready(self, key: str) -> bool:
        w = self.workers.get(key)
        return w is not None and w._connected

    async def start_all(self, config: dict, on_reply_cb=None):
        self._on_reply_cb = on_reply_cb
        worker_cfgs = config.get("workers", {})
        for key, wc in worker_cfgs.items():
            work_dir = wc.get("work_dir", "")
            name = wc.get("name", key)
            start_command = wc.get("start_command", "")
            worker = AcpWorker(key, work_dir, name=name, start_command=start_command)
            worker.on_reply = self._on_reply_cb
            self.add_worker(key, worker)

        # Start all workers concurrently
        tasks = [self.workers[k].start() for k in self.workers]
        if tasks:
            await asyncio.gather(*tasks)

    async def send_message(self, worker_key: str, text: str, qq_msg: dict = None):
        w = self.workers.get(worker_key)
        if w:
            await w.send_message(text, qq_msg)

    async def reset_for_msg(self, msg: dict) -> str:
        from .config import config, get_route
        route = get_route(msg["from_id"], msg["type"] == "private",
                          msg.get("is_mention", False))
        if not route:
            return "❌ 未匹配到路由"
        worker_name = route.get("worker", "")
        route_key = f"qq:{'private' if msg['type'] == 'private' else 'group'}:{msg['from_id']}"
        w = self.workers.get(worker_name)
        if not w:
            return f"❌ Worker [{worker_name}] 不存在"
        ok = await w.reset_route_session(route_key)
        if ok:
            return f"✅ [{route.get('name', '?')}] session 已重置"
        return "✅ session 已清空（无活跃 session）"

    async def stop_all(self):
        for w in self.workers.values():
            if w.proc:
                w.proc.kill()

    def status_lines(self) -> list[str]:
        lines = []
        for key, w in self.workers.items():
            status = "✅" if w._connected else "❌"
            sid = w.session_id or "-"
            sessions = len(w._route_sessions)
            start_cmd = getattr(w, 'start_command', '?') or '?'
            lines.append(f"  {status} {w.name or key} (session={sid[:16]}..., routes={sessions}) cmd={start_cmd}")
        return lines
