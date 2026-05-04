"""OneBot v11 protocol — message building, parsing, and API calls."""
from __future__ import annotations

import asyncio
import base64 as _b64
import json
import logging
import os
import re
import uuid
from typing import Optional

from . import config as cfg

log = logging.getLogger("onebot-bridge")


# ====== WS 回复 ======

async def send_api_action(action: str, params: dict = None,
                          ws: object = None) -> dict:
    ws = ws or cfg._ws_conn
    if ws is None:
        log.error("WS 未连接，无法发送 API 请求")
        return {}
    echo_id = str(uuid.uuid4())[:12]
    fut = asyncio.get_event_loop().create_future()
    cfg._echo_futures[echo_id] = fut
    payload = {"action": action, "params": params or {}, "echo": echo_id}
    await ws.send(json.dumps(payload, ensure_ascii=False))
    try:
        return await asyncio.wait_for(fut, timeout=10)
    except asyncio.TimeoutError:
        log.warning(f"API 超时: {action}")
        return {}


# ====== OneBot v11 消息段构建 ======

# 标签格式（支持在 message 内任意位置，URL 可跨行）
SEND_IMAGE_TAG_RE = re.compile(r"<img>(.*?)</img>", re.IGNORECASE | re.DOTALL)
SEND_AUDIO_TAG_RE = re.compile(r"<audio>(.*?)</audio>", re.IGNORECASE | re.DOTALL)
SEND_FILE_TAG_RE = re.compile(r"<file>(.*?)</file>", re.IGNORECASE | re.DOTALL)


def _build_message_segments(text: str) -> list:
    """将回复文本解析为 OneBot v11 消息段列表，支持标签格式"""
    # 提取所有标签中的 URL（去除空白和换行）
    image_urls = [u.strip().replace("\n", "").replace("\r", "") for u in SEND_IMAGE_TAG_RE.findall(text)]
    audio_urls = [u.strip().replace("\n", "").replace("\r", "") for u in SEND_AUDIO_TAG_RE.findall(text)]
    file_urls = [u.strip().replace("\n", "").replace("\r", "") for u in SEND_FILE_TAG_RE.findall(text)]

    # 移除所有标签
    clean = text
    for pat in [SEND_IMAGE_TAG_RE, SEND_AUDIO_TAG_RE, SEND_FILE_TAG_RE]:
        clean = pat.sub("", clean)
    clean = clean.strip()

    segments = []
    if clean:
        segments.append({"type": "text", "data": {"text": clean[:2000]}})
    for url in image_urls:
        if os.path.isfile(url):
            try:
                with open(url, "rb") as f:
                    raw = f.read()
                b64 = _b64.b64encode(raw).decode()
                segments.append({"type": "image", "data": {"file": f"base64://{b64}"}})
                log.info(f"📎 本地图片已转为 base64 ({len(raw)} bytes)")
            except Exception as e:
                log.warning(f"📎 本地图片读取失败: {e}")
                segments.append({"type": "text", "data": {"text": f"[图片读取失败: {url}]"}})
        else:
            segments.append({"type": "image", "data": {"url": url}})
    for url in audio_urls:
        segments.append({"type": "record", "data": {"url": url}})
    for url in file_urls:
        if os.path.isfile(url):
            try:
                with open(url, "rb") as f:
                    raw = f.read()
                fname = url.split("/")[-1][:64] or "file"
                b64 = _b64.b64encode(raw).decode()
                segments.append({"type": "file", "data": {"file": f"base64://{b64}", "name": fname}})
                log.info(f"📎 本地文件已转为 base64 ({len(raw)} bytes, name={fname})")
            except Exception as e:
                log.warning(f"📎 本地文件读取失败: {e}")
                segments.append({"type": "text", "data": {"text": f"[文件发送失败: {url}]"}})
        else:
            segments.append({"type": "file", "data": {"url": url, "name": url.split("/")[-1][:64] or "file"}})
    if not segments:
        segments.append({"type": "text", "data": {"text": "(无内容)"}})
    return segments


async def send_group_msg(group_id: int, text: str) -> bool:
    segments = _build_message_segments(text)
    result = await send_api_action("send_group_msg", {
        "group_id": group_id,
        "message": segments,
    })
    return bool(result.get("status") == "ok")


async def send_private_msg(user_id: int, text: str) -> bool:
    segments = _build_message_segments(text)
    result = await send_api_action("send_private_msg", {
        "user_id": user_id,
        "message": segments,
    })
    return bool(result.get("status") == "ok")


# ====== 回复回调（IO 链路独立）=====

async def on_worker_reply(worker_key: str, agent_name: str,
                          reply_text: str, qq_msg: dict):
    if not reply_text or not qq_msg:
        return

    log.info(f"[{agent_name}] reply → {qq_msg.get('type','?')} "
             f"{qq_msg.get('from_id','?')}: {reply_text[:60]}")

    user_id = int(qq_msg.get("user_id", 0))
    from_id = qq_msg.get("from_id", "")

    try:
        if qq_msg["type"] == "group":
            if not from_id or from_id == "TEST_USER_ID":
                log.warning(f"跳过测试群消息: from_id={from_id}")
                return
            await send_group_msg(int(from_id), reply_text)
        elif qq_msg["type"] == "private":
            if not user_id or user_id == 0:
                log.warning(f"跳过测试私聊: user_id={qq_msg.get('user_id')}")
                return
            await send_private_msg(user_id, reply_text)
    except (ValueError, TypeError) as e:
        log.warning(f"跳过无效消息 (user_id={qq_msg.get('user_id')} from_id={from_id}): {e}")
    except Exception as e:
        log.error(f"发送回复失败: {e}")


# ====== OneBot v11 协议解析 ======

def parse_onebot(data: dict) -> Optional[dict]:
    qq_cfg = cfg.config.get("applications", {}).get("qq", {})
    bot_qq = str(qq_cfg.get("bot_qq", ""))
    bot_name = str(qq_cfg.get("bot_name", ""))

    msg_type = data.get("message_type", "")
    user_id = str(data.get("user_id", ""))
    raw_msg = data.get("raw_message", "")

    if not raw_msg:
        return None

    sender = data.get("sender", {})
    sender_name = sender.get("nickname", "") or sender.get("card", "") or f"QQ{user_id}"

    is_mention = False
    message = raw_msg.strip()

    if msg_type == "group":
        group_id = str(data.get("group_id", ""))
        # Check for @mention via CQ at
        for mention in data.get("message", []):
            if mention.get("type") == "at":
                target = str(mention.get("data", {}).get("qq", ""))
                if target == bot_qq or target == "all":
                    is_mention = True
                    break
        # Fallback: text-based @detection
        if not is_mention:
            if f"@{bot_name}" in message or f"@{bot_qq}" in message:
                is_mention = True
        # Strip @mention prefix
        if is_mention:
            message = re.sub(r'\u0040' + re.escape(bot_name), '', message).strip()
            message = re.sub(rf'\[CQ:at,qq={re.escape(bot_qq)}\]', '', message).strip()
        from_id = group_id
    elif msg_type == "private":
        from_id = user_id
    else:
        return None

    return {
        "type": msg_type,
        "user_id": user_id,
        "from_id": from_id,
        "sender_name": sender_name,
        "message": message,
        "is_mention": is_mention,
        "raw": data,
    }
