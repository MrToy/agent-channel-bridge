"""ACP Worker — manages a single ACP agent subprocess."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Optional

from .config import config
from .rpc_log import log_rpc

log = logging.getLogger("onebot-bridge")



# ====== ACP Worker 通信 ======


class AcpWorker:
    def __init__(self, key: str, work_dir: str, name: str = "", start_command: str = ""):
        self.key = key
        self.name = name
        self.work_dir = work_dir
        self.agent_name = name
        self.start_command = start_command
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.session_id: Optional[str] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._connected = False
        self._msg_id = 0
        # 实例级变量，每个 Worker 独立，避免并发冲突
        self._pending_requests: dict[str, asyncio.Future] = {}
        self._session_buf: dict[str, str] = {}
        # 会话路由: route_key → session_id
        self._route_sessions: dict[str, str] = {}
        # sessionId → 最后一条 qq_msg（用于回复路由）
        self._last_qq_msg: dict[str, dict] = {}
        # sessionId → 积压消息列表 [(text, qq_msg), ...]
        self._pending_msgs: dict[str, list[tuple[str, dict]]] = {}
        # msg_id → sessionId (用来在 prompt result 时反查 session)
        self._prompt_msg_map: dict[str, str] = {}
        # 回调: on_reply(worker_name, reply_text, qq_msg)
        self.on_reply = None
        # session 持久化路径
        self._session_file = os.path.join(work_dir, ".bridge_sessions.json")

    def _save_sessions(self):
        """保存 session 映射到文件"""
        import json as _json
        try:
            data = {rk: sid for rk, sid in self._route_sessions.items()}
            with open(self._session_file, "w") as f:
                _json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            log.warning(f"[{self.agent_name}] session 持久化写入失败: {e}")

    async def _restore_sessions(self):
        """从文件恢复 session，逐个尝试 resume"""
        import json as _json
        try:
            if not os.path.isfile(self._session_file):
                return
            with open(self._session_file) as f:
                data = _json.load(f)
            if not isinstance(data, dict):
                return
            restored = {}
            for route_key, sid in data.items():
                try:
                    await self._send_request("session/resume", {
                        "sessionId": sid,
                        "cwd": self.work_dir,
                    })
                    restored[route_key] = sid
                    log.info(f"[{self.agent_name}] 🔄 恢复 Session [{route_key}]: {sid}")
                except Exception as e:
                    log.info(f"[{self.agent_name}] ⏭ Session [{route_key}] 恢复失败，后续会重建: {e}")
            if restored:
                self._route_sessions.update(restored)
                log.info(f"[{self.agent_name}] ✅ 恢复 {len(restored)}/{len(data)} 个 session")
        except Exception as e:
            log.warning(f"[{self.agent_name}] session 恢复过程异常: {e}")

    async def start(self):
        if not self.start_command:
            log.error(f"[{self.agent_name}] ❌ start_command 未配置")
            return
        cmd = self.start_command.split()
        log.info(f"[{self.agent_name}] 启动: {' '.join(cmd)}")

        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self.work_dir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "HOME": os.environ.get("HOME", "/home/ubuntu")},
        )

        self._stderr_task = asyncio.create_task(self._read_stderr())
        self._reader_task = asyncio.create_task(self._read_stdout())

        await self._send_request("initialize", {
            "protocolVersion": 6,
            "clientCapabilities": {
                "fs": {"readTextFile": True, "writeTextFile": True},
                "terminals": {"create": True, "output": True, "release": True,
                              "waitForExit": True, "kill": True},
                "prompts": {"image": True},
            },
            "clientInfo": {
                "name": "onebot-bridge",
                "title": "OneBot Bridge",
                "version": "1.0.0",
            },
        })
        log.info(f"[{self.agent_name}] ✅ ACP 初始化完成")
        self._connected = True

        # 尝试恢复持久化的 session
        await self._restore_sessions()

    async def _read_stderr(self):
        try:
            while self.proc and self.proc.stderr and not self.proc.stderr.at_eof():
                line = await self.proc.stderr.readline()
                if line:
                    text = line.decode(errors='replace').strip()
                    if text:
                        log.info(f"[{self.agent_name} stderr] {text}")
        except Exception:
            pass

    async def _read_stdout(self):
        try:
            buf = b""
            while self.proc and self.proc.stdout and not self.proc.stdout.at_eof():
                chunk = await self.proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                        await self._handle_message(msg)
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            log.error(f"[{self.agent_name}] stdout 读取错误: {e}")

    async def _handle_message(self, msg: dict):
        log_rpc(self.agent_name, "<<", msg)
        # DEBUG: 打印所有消息
        if "id" in msg:
            log.info(f"[{self.agent_name}] 📩 RPC id={msg['id']} keys={list(msg.keys())} {'result' in msg} has_stopReason={'stopReason' in msg.get('result',{})}")
        elif msg.get("method") == "session/update":
            ut = msg.get("params", {}).get("update", {}).get("sessionUpdate", "")
            if ut in ("agent_message_chunk", "step_finish", "tool_call_start"):
                log.info(f"[{self.agent_name}] 📩 UPDATE type={ut}")

        # JSON-RPC 回复
        if "id" in msg:
            msg_id = str(msg["id"])
            is_prompt_result = (
                msg.get("id") is not None
                and "result" in msg
                and isinstance(msg["result"], dict)
                and "stopReason" in msg["result"]
            )
            
            if is_prompt_result:
                # session/prompt 的 result 中包含 stopReason → 回复完成
                reason = msg["result"].get("stopReason", "")
                usage = msg["result"].get("usage", {})
                cost = msg["result"].get("cost", 0)
                log.info(f"[{self.agent_name}] 🏁 prompt 完成 (reason={reason}, cost={cost})")
                # 清除 prompt_msg_map 记录
                self._prompt_msg_map.pop(msg_id, None)
            
            # 普通 RPC 回复
            fut = self._pending_requests.pop(msg_id, None)
            if fut and not fut.done():
                if "result" in msg:
                    fut.set_result(msg["result"])
                elif "error" in msg:
                    fut.set_exception(
                        Exception(msg["error"].get("message", str(msg["error"])))
                    )
            return

        # session/update 通知（收集文本块）
        if msg.get("method") == "session/update":
            await self._handle_session_update(msg.get("params", {}))

    async def _handle_session_update(self, params: dict):
        update = params.get("update", {})
        update_type = update.get("sessionUpdate", "")
        content = update.get("content", {})

        # 从通知中获取 sessionId
        sid = params.get("sessionId", "")
        if not sid:
            return

        # 只关注最终输出的文本块
        if update_type == "agent_message_chunk":
            if content.get("type") == "text":
                text = content.get("text", "")
                clean = text
                for ch in ["┃", "╹", "▣", "■", "▌", "▐", "▀", "▄", "░", "▒", "▓",
                           "│", "║", "═", "╔", "╗", "╚", "╝", "╠", "╣", "╦", "╩", "╬"]:
                    clean = clean.replace(ch, "")
                if clean.strip():
                    self._session_buf.setdefault(sid, "")
                    self._session_buf[sid] += clean

                    # 检测到 </message> → 立即发送
                    if "</message>" in self._session_buf[sid]:
                        full = self._session_buf.pop(sid, "")
                        import re as _re
                        msgs = _re.findall(r'<message>(.*?)</message>', full, _re.DOTALL)
                        qq_msg = self._last_qq_msg.get(sid)
                        for mtext in msgs:
                            mtext = mtext.strip()
                            if not mtext:
                                continue
                            log.info(f"[{self.agent_name}] 📬 条: {mtext[:60]}")
                            if qq_msg and self.on_reply:
                                asyncio.create_task(self.on_reply(
                                    self.name, self.agent_name, mtext, qq_msg
                                ))

    async def send_message(self, text: str, qq_msg: dict = None):
        """发送消息到 agent，立即返回，不等待回复"""
        if not self._connected:
            log.warning(f"[{self.agent_name}] ACP 未就绪")
            return

        # 按来源确定 route_key
        if qq_msg:
            is_private = qq_msg.get("type") == "private"
            route_key = f"qq:private:{qq_msg['user_id']}" if is_private else f"qq:group:{qq_msg['from_id']}"
        else:
            route_key = "_default"

        # 获取或创建 session
        sid = self._route_sessions.get(route_key)
        if not sid:
            try:
                result = await self._send_request("session/new", {
                    "cwd": self.work_dir,
                    "mcpServers": [],
                })
                sid = result["sessionId"]
            except Exception as e:
                log.error(f"[{self.agent_name}] ❌ session/new 失败: {e}", exc_info=True)
                return
            self._route_sessions[route_key] = sid
            self._save_sessions()
            log.info(f"[{self.agent_name}] 📝 新 Session [{route_key}]: {sid}")

        self._last_qq_msg[sid] = qq_msg or {}
        self._session_buf.pop(sid, None)  # 清除上一轮残留

        # 检查 session 是否忙碌（有正在处理的 prompt）
        if sid in self._prompt_msg_map.values():
            # session 正在处理中 → close 旧 session，重建新 session
            log.info(f"[{self.agent_name}] 🔌 Session [{route_key}] 忙碌，准备打断重建")
            try:
                await self._send_request("session/close", {"sessionId": sid})
            except Exception as e:
                log.warning(f"[{self.agent_name}] session/close [{route_key}] 失败: {e}")
            del self._route_sessions[route_key]
            self._pending_msgs.pop(sid, None)
            self._session_buf.pop(sid, None)
            for mid in list(self._prompt_msg_map.keys()):
                if self._prompt_msg_map[mid] == sid:
                    del self._prompt_msg_map[mid]

            # 重建新 session
            try:
                result = await self._send_request("session/new", {
                    "cwd": self.work_dir,
                    "mcpServers": [],
                })
                sid = result["sessionId"]
            except Exception as e:
                log.error(f"[{self.agent_name}] ❌ session/new 失败: {e}", exc_info=True)
                return
            self._route_sessions[route_key] = sid
            self._save_sessions()
            log.info(f"[{self.agent_name}] 📝 重建 Session [{route_key}]: {sid}")

        self._last_qq_msg[sid] = qq_msg or {}
        self._session_buf.pop(sid, None)  # 清除上一轮残留

        await self._do_send_prompt(sid, route_key, text, qq_msg)

    async def _do_send_prompt(self, sid: str, route_key: str, text: str, qq_msg: dict = None):
        """构造系统上下文并发起 session/prompt，同时记录 prompt msg_id → sessionId 映射"""
        # ===== 构造系统上下文 + 用户消息 =====
        ctx_lines = ["<message>"]
        ctx_lines.append("<tips>")

        # 平台信息
        src = "群聊" if qq_msg and qq_msg.get("type") == "group" else "私聊"
        sender = qq_msg.get("sender_name", "用户") if qq_msg else "用户"
        user_id = qq_msg.get("user_id", "") if qq_msg else ""
        ctx_lines.append(f"你正在通过QQ和用户对话。来源: {src}，发送者: {sender}。")
        if qq_msg and qq_msg.get("type") == "group" and user_id:
            ctx_lines.append(f"⚠️ 重要：在群聊中回复时，第一行必须用 @用户QQ号 开头！否则用户收不到提示。")
            ctx_lines.append(f"   例如回复 \"@{user_id} 你好呀～\"")

        # 图片
        if qq_msg and qq_msg.get("has_image"):
            ctx_lines.append(f"用户同时发送了 {len(qq_msg.get('images', []))} 张图片:")
            for i, img in enumerate(qq_msg.get("images", [])):
                url = img.get("url", "")
                if url:
                    ctx_lines.append(f"  图片{i+1}: {url}")

        # 特殊格式指令
        ctx_lines.append("")
        ctx_lines.append("【回复规则】")
        ctx_lines.append("  1. 每条独立回复用 <message> 包裹，支持一次输出多条：")
        ctx_lines.append("     <message>")
        ctx_lines.append("     第一条回复")
        ctx_lines.append("     </message>")
        ctx_lines.append("     <message>")
        ctx_lines.append("     第二条回复")
        ctx_lines.append("     </message>")
        ctx_lines.append("  2. 每条 <message> 输出后立即发送给用户，无需等待")
        ctx_lines.append("  3. 群聊时每条 <message> 第一行必须 @用户QQ号")
        ctx_lines.append("  4. 思考过程、内部推理不要用 <message> 包裹，不会发给用户")
        ctx_lines.append("")
        ctx_lines.append("【发送图片/文件/语音 - 标签格式】")
        ctx_lines.append("  1. 在 <message> 内的任意位置插入标签即可发送媒体：")
        ctx_lines.append("     <message>这是查询结果<img>https://example.com/result.png</img></message>")
        ctx_lines.append("  2. <img>URL</img> — 发送图片")
        ctx_lines.append("  3. <audio>URL</audio> — 发送语音")
        ctx_lines.append("  4. <file>URL</file> — 发送文件")
        ctx_lines.append("  5. ⚠️ 标签可以放在 message 内的任何位置，文字前后中间都行")
        ctx_lines.append("  6. ⚠️ 如果你用 webfetch/grab/curl 等工具下载了图片或生成了本地图片文件：")
        ctx_lines.append("     • <img>https://example.com/pic.png</img> — 远程 URL")
        ctx_lines.append("     • <img>/absolute/path/to/file.png</img> — 本地绝对路径（自动转 base64 发送）")
        ctx_lines.append("     • <img>base64://iVBORw0KGgo...</img> — base64 编码（太长不建议）")
        ctx_lines.append("")
        ctx_lines.append("【进度同步约定】")
        ctx_lines.append("  ⚠️ 以下场景**必须**输出 <message> 告知用户进度：")
        ctx_lines.append("  1. **收到用户消息后，立即输出一条确认消息** <message> 再开始工作")
        ctx_lines.append("     例如: <message>收到～我来看看...</message>")
        ctx_lines.append("  2. 开始执行重要工具/命令前 — 告诉用户你要做什么")
        ctx_lines.append("  3. 用户指令执行完毕时 — 告知结果或下一步")
        ctx_lines.append("  4. 工具执行慢或卡住时 — 告知用户 '正在执行，请稍候'")
        ctx_lines.append("  5. 遇到问题需要用户决策时 — 询问意见")
        ctx_lines.append("  6. 长时间无文字输出时（超过10秒）— 必须输出一条进度")
        ctx_lines.append("")
        ctx_lines.append("  注意：思考过程和内部推理不要用 <message> 包裹，这些不会发给用户")

        ctx_lines.append("</tips>")
        ctx_lines.append("")
        ctx_lines.append(text)
        ctx_lines.append("</message>")

        prompt_text = "\n".join(ctx_lines)

        # 用 request 发 prompt，但后台等 result，不阻塞这里
        self._send_request_bg("session/prompt", {
            "sessionId": sid,
            "prompt": [{"type": "text", "text": prompt_text}],
        }, sid=sid)
        log.info(f"[{self.agent_name}] 📤 消息已发送: {text[:60]}{' 📷' if qq_msg and qq_msg.get('has_image') else ''}")

    def _send_request_bg(self, method: str, params: dict, sid: str = ""):
        """后台发送 JSON-RPC 请求，不等待回复"""
        self._msg_id += 1
        msg_id = str(self._msg_id)
        if sid and method == "session/prompt":
            self._prompt_msg_map[msg_id] = sid
        msg = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
            "params": params,
        }
        log_rpc(self.agent_name, ">>", msg)
        if self.proc and self.proc.stdin:
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            asyncio.create_task(self.proc.stdin.drain())

    async def _send_request(self, method: str, params: dict) -> dict:
        self._msg_id += 1
        msg_id = str(self._msg_id)
        msg = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
            "params": params,
        }
        log_rpc(self.agent_name, ">>", msg)
        fut = asyncio.get_event_loop().create_future()
        self._pending_requests[msg_id] = fut

        if self.proc and self.proc.stdin:
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            await self.proc.stdin.drain()

        try:
            return await asyncio.wait_for(fut, timeout=30)
        except asyncio.TimeoutError:
            self._pending_requests.pop(msg_id, None)
            raise TimeoutError(f"[{self.agent_name}] RPC 超时: {method}")


    async def _send_notification(self, method: str, params: dict):
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        log_rpc(self.agent_name, ">>", msg)
        if self.proc and self.proc.stdin:
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            await self.proc.stdin.drain()

    async def reset_sessions(self) -> dict:
        """关闭所有 session 并清空路由映射"""
        self._save_sessions()  # 先保存当前状态，万一关机恢复
        closed = 0
        errors = 0
        for route_key, sid in list(self._route_sessions.items()):
            try:
                await self._send_request("session/close", {
                    "sessionId": sid,
                })
                closed += 1
            except Exception as e:
                log.warning(f"[{self.agent_name}] session/close [{route_key}] 失败: {e}")
                errors += 1
            del self._route_sessions[route_key]
        self._save_sessions()
        log.info(f"[{self.agent_name}] 🧹 已重置 {closed} 个 session{'，' + str(errors) + ' 个失败' if errors else ''}")
        return {"closed": closed, "errors": errors}

    async def reset_route_session(self, route_key: str) -> bool:
        """关闭指定 route_key 的 session"""
        sid = self._route_sessions.pop(route_key, None)
        if not sid:
            return False
        try:
            await self._send_request("session/close", {
                "sessionId": sid,
            })
            self._save_sessions()
            log.info(f"[{self.agent_name}] 🧹 已重置 session [{route_key}]: {sid}")
            return True
        except Exception as e:
            log.warning(f"[{self.agent_name}] session/close [{route_key}] 失败: {e}")
            return False

    @property
    def connected(self) -> bool:
        return self._connected

    async def stop(self):
        self._connected = False
        for task in [self._reader_task, self._stderr_task]:
            if task:
                task.cancel()
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.proc.kill()



