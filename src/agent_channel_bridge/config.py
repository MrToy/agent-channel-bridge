"""Configuration loading, routing, and chat logging."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Optional

import yaml

log = logging.getLogger("onebot-bridge")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")
CHAT_LOG_DIR = os.path.join(BASE_DIR, "chat_logs")

config: dict = {}
_ws_conn: Optional[object] = None
_echo_futures: dict[str, object] = {}


# ====== 配置加载 ======

def load_config():
    global config
    with open(CONFIG_PATH) as f:
        loaded = yaml.safe_load(f)
    config.clear()
    config.update(loaded)
    log.info(f"配置已加载: {len(config.get('workers', {}))} 个 worker")


# ====== 路由匹配 ======

def get_route(from_id: str, is_private: bool = False,
              is_mention: bool = False) -> Optional[dict]:
    defaults = config.get("default", {})
    route_key = f"qq:private:{from_id}" if is_private else f"qq:group:{from_id}"
    routes = config.get("routes", {})

    if route_key in routes:
        if is_private or is_mention:
            r = routes[route_key]
            worker_name = r.get("worker", defaults.get("worker", ""))
            return {"name": r.get("name", route_key), "worker": worker_name}

    if is_private:
        worker_name = defaults.get("worker", "")
        if worker_name:
            return {"name": "默认私聊", "worker": worker_name}

    if not is_private and is_mention:
        worker_name = defaults.get("worker", "")
        if worker_name:
            return {"name": "默认群聊@", "worker": worker_name}

    return None


# ====== 聊天日志 ======

def log_chat(msg: dict, route_name: str = ""):
    date = datetime.now().strftime("%Y%m%d")
    path = os.path.join(CHAT_LOG_DIR, f"{date}.log")
    os.makedirs(CHAT_LOG_DIR, exist_ok=True)
    entry = {
        "time": datetime.now().isoformat(),
        "type": msg["type"],
        "from": msg["from_id"],
        "sender": msg["sender_name"],
        "user": msg["user_id"],
        "route": route_name,
        "msg": msg["message"],
    }
    with open(path, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
