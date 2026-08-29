#!/usr/bin/env python3
"""
直接从 GitHub 下载 socks5-otc.txt 代理节点
无需 TG 抓取，无需 9router 登录
"""

import os
import re
import sys
import logging
import requests
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# GitHub 上的 socks5-otc.txt 地址
PROXY_FILE_URL = "https://raw.githubusercontent.com/yutian81/Keepalive/main/9r-proxy/socks5-otc.txt"
OUTPUT_FILE = "socks5-otc.txt"

# 并行测试配置
TEST_CONCURRENCY = int(os.getenv("TEST_CONCURRENCY") or "8")
TEST_TIMEOUT = int(os.getenv("TEST_TIMEOUT") or "5")

# 解析节点 URL: scheme://user:pass@ip:port
NODE_RE = re.compile(r"(socks5|http)://[^\s#@]+@(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d+)")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("proxy-fetch")


def download_proxies():
    """从 GitHub 下载代理列表"""
    log.info("📥 正在从 GitHub 下载代理列表...")
    try:
        resp = requests.get(PROXY_FILE_URL, timeout=15)
        resp.raise_for_status()
        proxies = [line.strip() for line in resp.text.splitlines() if line.strip()]
        log.info("✅ 下载成功，共 %d 个代理节点", len(proxies))
        return proxies
    except Exception as e:
        log.error("❌ 下载失败: %s", e)
        return []


def test_single_proxy(proxy_url):
    """测试单个代理连通性"""
    proxies = {"http": proxy_url, "https": proxy_url}
    try:
        resp = requests.get(
            "https://api.ip.sb/ip",
            proxies=proxies,
            timeout=TEST_TIMEOUT,
            verify=False
        )
        if resp.status_code == 200:
            ip = resp.text.strip()
            return True, ip
    except Exception:
        pass
    return False, None


def test_proxies(proxies):
    """并行测试所有代理"""
    log.info("🔍 开始测试代理连通性 (并发: %d)...", TEST_CONCURRENCY)
    
    working = []
    failed = []
    
    with ThreadPoolExecutor(max_workers=TEST_CONCURRENCY) as executor:
        future_to_proxy = {executor.submit(test_single_proxy, p): p for p in proxies}
        
        for i, future in enumerate(as_completed(future_to_proxy), 1):
            proxy = future_to_proxy[future]
            ok, ip = future.result()
            if ok:
                working.append({"proxy": proxy, "ip": ip})
                log.info("[%d/%d] ✅ %s → %s", i, len(proxies), proxy[:50], ip)
            else:
                failed.append(proxy)
                log.info("[%d/%d] ❌ %s", i, len(proxies), proxy[:50])
    
    return working, failed


def send_tg_notification(stats):
    """发送 TG 通知汇总"""
    token = os.getenv("TG_BOT_TOKEN") or ""
    chat_id = os.getenv("TG_CHAT_ID") or ""
    if not token or not chat_id:
        log.info("ℹ️ 未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，跳过通知")
        return
    
    bjt = datetime.now(timezone(timedelta(hours=8)))
    date_str = f"{bjt.year}年{bjt.month:02d}月{bjt.day:02d}日"
    
    message = (
        f"🎉 <b>代理池更新完成</b>\n"
        f"----------------\n"
        f"📅 <b>日期</b>：{date_str}\n"
        f"📥 <b>GitHub 下载</b>：{stats['downloaded']} 个节点\n"
        f"✅ <b>可用</b>：{stats['working']} 个\n"
        f"❌ <b>不可用</b>：{stats['failed']} 个\n"
        f"📄 <b>已保存到</b>：{OUTPUT_FILE}"
    )
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        result = resp.json()
        if result.get("ok"):
            log.info("📩 TG 通知发送成功")
        else:
            log.warning("⚠️ TG 通知发送失败: %s", result.get("description"))
    except Exception as e:
        log.error("❌ TG 通知异常: %s", e)


def main():
    log.info("=" * 50)
    log.info("  代理池同步工具 (直接从 GitHub)")
    log.info("=" * 50)
    
    # 1. 下载代理列表
    proxies = download_proxies()
    if not proxies:
        log.error("没有可用的代理节点")
        sys.exit(1)
    
    # 2. 测试代理连通性
    working, failed = test_proxies(proxies)
    
    # 3. 保存可用代理到文件
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for item in working:
            f.write(item["proxy"] + "\n")
    log.info("📄 已保存 %d 个可用代理到 %s", len(working), OUTPUT_FILE)
    
    # 4. 发送通知
    stats = {
        "downloaded": len(proxies),
        "working": len(working),
        "failed": len(failed),
    }
    try:
        send_tg_notification(stats)
    except Exception as e:
        log.error("❌ 发送通知异常: %s", e)
    
    # 5. 输出汇总
    log.info("=" * 50)
    log.info("📊 汇总: 下载 %d, 可用 %d, 不可用 %d",
             stats["downloaded"], stats["working"], stats["failed"])
    log.info("=" * 50)
    
    # 输出可用代理列表
    if working:
        log.info("✅ 可用代理列表:")
        for item in working:
            log.info("  %s → %s", item["proxy"], item["ip"])


if __name__ == "__main__":
    main()
