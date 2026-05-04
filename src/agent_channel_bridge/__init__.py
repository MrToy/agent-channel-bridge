"""Agent Channel Bridge — 将 QQ 消息路由到 AI Coding Agent."""

import logging
import sys

__version__ = "1.0.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
    force=True,
)
