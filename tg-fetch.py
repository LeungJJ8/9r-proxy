#!/usr/bin/env python3
"""
TG 频道 socks5 节点抓取脚本（GitHub Actions 专用）

从公开频道 @otcfxq（OTC分享群）获取最近 3 天的 socks5/http 代理节点：
1. 正则提取 socks5:// 与 http(s):// 节点
2. 剔除 #标签、@otcfxq、说明文字等后缀
3. 以 ip:port 去重
4. 输出到 socks5.txt

需要的配置（环境变量）：
  TG_API_ID        Telegram API ID
  TG_API_HASH      Telegram API Hash
  TG_SESSION_STR   Telethon 登录会话字符串
  FETCH_DAYS       抓取最近 N 天，默认 3
"""

import os
import re
import sys
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient
from telethon.sessions import StringSession

# Windows 事件循环策略，兼容本地调试
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ================= 配置区域 =================
TG_API_ID = os.getenv("TG_API_ID") or ""
TG_API_HASH = os.getenv("TG_API_HASH") or ""
TG_SESSION_STR = os.getenv("TG_SESSION_STR") or ""
FETCH_DAYS = int(os.getenv("FETCH_DAYS") or "3")
OUTPUT_FILE = "socks5.txt"
CHANNEL = "@otcfxq"
# ============================================

# 匹配 socks5/http/https 节点 URL（user:pass@ip:port 或 ip:port）
# 示例: socks5://root:123456@59.36.149.183:1080#CN Wancheng... @otcfxq
# [^\s#@]+ 在 # 前停止 → match.group(0) 即纯净 URL（不含 #标签/@otcfxq）
NODE_RE = re.compile(
    r"(socks5|http|https)://[^\s#@]+@(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d+)"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("tg-fetch")


def extract_nodes(text: str) -> list:
    """从消息文本中提取纯净节点 URL 列表（去重由调用方完成）"""
    return [m.group(0) for m in NODE_RE.finditer(text)]


async def main():
    if not TG_API_ID or not TG_API_HASH:
        log.error("缺少 TG_API_ID 或 TG_API_HASH，请检查环境变量")
        sys.exit(1)
    if not TG_SESSION_STR:
        log.error("缺少 TG_SESSION_STR，请先运行 tg-session.py 获取会话字符串")
        sys.exit(1)

    log.info("=" * 48)
    log.info("TG 节点抓取启动")
    log.info("目标频道: %s", CHANNEL)
    log.info("抓取范围: 最近 %d 天", FETCH_DAYS)

    client = TelegramClient(
        StringSession(TG_SESSION_STR), int(TG_API_ID), TG_API_HASH
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            log.error("TG 会话已失效，请更新 TG_SESSION_STR")
            sys.exit(1)

        entity = await client.get_entity(CHANNEL)
        log.info("已连接频道: %s", getattr(entity, "title", CHANNEL))

        cutoff = datetime.now(timezone.utc) - timedelta(days=FETCH_DAYS)
        seen = {}
        total_messages = 0
        async for msg in client.iter_messages(entity, offset_date=cutoff):
            total_messages += 1
            if not msg.text:
                continue
            for node in extract_nodes(msg.text):
                # 去重: 以 ip:port 为 key（保留首次出现的完整 URL）
                ip_port = NODE_RE.match(node)
                key = f"{ip_port.group(2)}:{ip_port.group(3)}" if ip_port else node
                seen.setdefault(key, node)

        log.info("扫描消息数: %d", total_messages)
        log.info("提取节点数: %d", len(seen))

        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            for node in seen.values():
                f.write(node + "\n")

        log.info("已写入 %s (%d 个节点)", OUTPUT_FILE, len(seen))
        log.info("=" * 48)
        if not seen:
            sys.exit(1)  # 未抓取到节点，判定为失败
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
