"""Entry point — admin commands, message handler, main loop."""
from __future__ import annotations

import asyncio
import json
import logging
import traceback
from typing import Optional

import websockets

from . import config as cfg
from .rpc_log import init_rpc_log
from .onebot import on_worker_reply, send_group_msg, send_private_msg, parse_onebot
from .worker_manager import WorkerManager
from .config import get_route, log_chat

log = logging.getLogger("onebot-bridge")


# ====== 管理命令 ======

def is_admin(msg: dict) -> bool:
    route_key = f"qq:{msg['type']}:{msg['from_id']}"
    routes = cfg.config.get("routes", {})
    route = routes.get(route_key)
    return bool(route and route.get("admin"))


async def handle_admin_cmd(msg: dict, worker_mgr: WorkerManager) -> Optional[str]:
    t = msg["message"].strip()
    cmd = t.split()[0] if t else ""

    if cmd == "/help":
        return (
            "📋 管理命令:\n"
            "/status          - worker 状态\n"
            "/reset           - 重置当前会话 session\n"
            "/help            - 本帮助"
        )

    if cmd == "/status":
        lines = ["📊 Workers (ACP):"] + worker_mgr.status_lines()
        return "\n".join(lines)

    if cmd == "/reset":
        reply = await worker_mgr.reset_for_msg(msg)
        return reply

    return None


# ====== 消息处理 ======

async def process_message(ws, msg: dict, worker_mgr: WorkerManager):
    log.info(f"[{msg['type']}] {msg['from_id']}|{msg['sender_name']}: {msg['message'][:60]}")

    # 管理命令
    if is_admin(msg):
        reply = await handle_admin_cmd(msg, worker_mgr)
        if reply:
            if msg["type"] == "group":
                await send_group_msg(int(msg["from_id"]), reply)
            else:
                await send_private_msg(int(msg["user_id"]), reply)
            return

    # 路由匹配
    route = get_route(msg["from_id"], msg["type"] == "private",
                      msg.get("is_mention", False))
    if not route:
        log.warning(f"⚠️ 未匹配到路由: from={msg['from_id']} type={msg['type']} "
                    f"mention={msg.get('is_mention')} msg={msg['message'][:40]}")
        return

    worker_name = route.get("worker", "")
    route_name = route.get("name", "?")

    if not worker_mgr.is_ready(worker_name):
        log.warning(f"[{route_name}] Worker [{worker_name}] 未就绪")
        return

    # 发送到 agent（异步，不等回复）
    log.info(f"⚡ {route_name} → ACP [{worker_name}]")
    await worker_mgr.send_message(worker_name, msg["message"], qq_msg=msg)

    log_chat(msg, route_name)


# ====== WS 主循环 ======

async def main_loop(worker_mgr: WorkerManager):
    ws_url = cfg.config.get("applications", {}).get("qq", {}).get("ws_url", "ws://localhost:3001")

    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=30) as ws:
                cfg._ws_conn = ws
                log.info(f"✅ 已连接 {ws_url}")

                async for raw in ws:
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    # 1. WS API 回复（echo 匹配）
                    echo_id = data.get("echo", "")
                    if echo_id and echo_id in cfg._echo_futures:
                        fut = cfg._echo_futures.pop(echo_id, None)
                        if fut and not fut.done():
                            fut.set_result(data)
                        continue

                    # 2. OneBot 消息事件
                    if data.get("post_type") == "message":
                        msg = parse_onebot(data)
                        if msg:
                            asyncio.create_task(process_message(ws, msg, worker_mgr))

        except websockets.ConnectionClosed:
            log.warning("断开，3s 重连...")
            await asyncio.sleep(3)
        except Exception as e:
            log.error(f"WS 错误: {e}")
            traceback.print_exc()
            await asyncio.sleep(5)


# ====== 入口 ======

async def main():
    cfg.load_config()
    init_rpc_log()
    log.info("=" * 45)
    log.info("🚀 OneBot Bridge (ACP 协议版)")
    log.info(f"  WS: {cfg.config.get('applications', {}).get('qq', {}).get('ws_url', 'ws://localhost:3001')}")
    log.info(f"  Workers: {len(cfg.config.get('workers', {}))}")
    log.info("  异步非阻塞 | IO 链路独立")
    log.info("=" * 45)

    worker_mgr = WorkerManager()

    log.info("启动 ACP agent workers...")
    await worker_mgr.start_all(cfg.config, on_reply_cb=on_worker_reply)

    log.info("当前 worker 状态:")
    for line in worker_mgr.status_lines():
        log.info(line)

    await main_loop(worker_mgr)


def entry():
    """Entry point for console_scripts."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bridge 已停止")
    except Exception as e:
        log.error(f"崩溃: {e}")
        raise


if __name__ == "__main__":
    entry()
