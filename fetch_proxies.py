#!/usr/bin/env python3
"""
代理抓取脚本（第一步）

从 proxy-socks5.com 登录抓取代理列表，过滤带 x/X 掩码的 IP，
本地测试连通性后，输出可用代理列表。

输入（环境变量）：
  PROXY_USER    proxy-socks5.com 登录用户名
  PROXY_PASS    proxy-socks5.com 登录密码

输出：
  proxies.txt   测试通过的可用代理列表（protocol://ip:port 每行一个）
"""

import os
import re
import sys
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter

import requests

# proxy-socks5.com 配置
PROXY_SITES_LOGIN_URL = "http://proxy-socks5.com/login"
PROXY_SITES_PROXY_LIST_URL = "http://proxy-socks5.com/proxy_list"
PROXY_USER = os.getenv("PROXY_USER") or ""
PROXY_PASS = os.getenv("PROXY_PASS") or ""

# 输出文件
OUTPUT_FILE = os.getenv("PROXY_OUTPUT") or "proxies.txt"

# 只处理这些类型
TYPE_ALLOWED = {"socks5", "http"}

# 本地连通性测试配置
TEST_URL = os.getenv("PROXY_TEST_URL") or "http://www.gstatic.com/generate_204"
TEST_CONCURRENCY = int(os.getenv("TEST_CONCURRENCY") or "20")
TEST_TIMEOUT = int(os.getenv("TEST_TIMEOUT") or "8")

# 解析节点 URL: scheme://user:pass@ip:port
NODE_RE = re.compile(r"(socks4|socks5|http)s?://(?:[^\s#@]+@)?(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d+)")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fetch-proxies")

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': PROXY_SITES_LOGIN_URL
}


def login_and_fetch():
    """登录 proxy-socks5.com 并获取代理列表页面 HTML"""
    session = requests.Session()

    # 1. 访问登录页
    resp = session.get(PROXY_SITES_LOGIN_URL, headers=HEADERS, timeout=15)
    if resp.status_code != 200:
        log.error("访问登录页失败: %s", resp.status_code)
        return ""

    # 2. 提交登录
    login_data = {'username': PROXY_USER, 'password': PROXY_PASS}
    login_resp = session.post(PROXY_SITES_LOGIN_URL, data=login_data, headers=HEADERS, allow_redirects=False, timeout=15)
    if login_resp.status_code in (200, 302):
        log.info("✅ proxy-socks5.com 登录成功")

        # 3. 获取代理列表
        resp = session.get(PROXY_SITES_PROXY_LIST_URL, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            return resp.text

    log.error("登录或获取代理列表失败")
    return ""


def has_x_ip(proxy):
    """检查代理 IP 是否包含 x/X（掩码隐藏的 IP）"""
    m = NODE_RE.match(proxy)
    if not m:
        return False
    ip = m.group(2)
    return 'x' in ip.lower()


def parse_proxies_from_html(html):
    """解析代理列表，提取协议类型 + IP:Port，并过滤掉带 x/X 的 IP"""
    proxies = []
    seen = set()

    # 方法1: 从 badge-type + data-proxy 匹配
    card_pattern = r'<div class="proxy-card"[^>]*data-proxy="([^"]+)"[^>]*>.*?badge badge-type[^>]*>(\w+)<'
    for match in re.finditer(card_pattern, html, re.DOTALL):
        ip_port = match.group(1)
        protocol = match.group(2).lower()
        proxy = f"{protocol}://{ip_port}"
        if proxy not in seen and not has_x_ip(proxy):
            seen.add(proxy)
            proxies.append(proxy)

    # 方法2: 从 table tbody 解析
    if not proxies:
        tbody_match = re.search(r'<tbody>(.*?)</tbody>', html, re.DOTALL)
        if tbody_match:
            rows = re.findall(r'<tr[^>]*>(.*?)</tr>', tbody_match.group(1), re.DOTALL)
            for row in rows:
                type_match = re.search(r'badge badge-type[^>]*>(\w+)<', row)
                # 提取 IP
                ip_match = re.search(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})', row)
                # 提取端口
                port_match = re.search(r'<td[^>]*>(\d+)</td>', row)

                if type_match and ip_match:
                    protocol = type_match.group(1).lower()
                    ip = ip_match.group(1)
                    port = port_match.group(1) if port_match else "1080"
                    proxy = f"{protocol}://{ip}:{port}"
                    if proxy not in seen and not has_x_ip(proxy):
                        seen.add(proxy)
                        proxies.append(proxy)

    return proxies


def test_proxy(proxy):
    """本地测试单个代理连通性"""
    proto = proxy.split("://")[0]
    proxies_dict = {
        "http": proxy,
        "https": proxy,
    }
    try:
        resp = requests.get(
            TEST_URL,
            proxies=proxies_dict,
            timeout=TEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        return proxy, resp.status_code == 204 or resp.status_code == 200
    except Exception:
        return proxy, False


def test_proxies(proxies):
    """并行本地测试所有代理，返回可用列表"""
    log.info("🔍 开始本地测试 %d 个代理（并发 %d，超时 %ds，目标 %s）...",
             len(proxies), TEST_CONCURRENCY, TEST_TIMEOUT, TEST_URL)

    alive = []
    dead = []
    with ThreadPoolExecutor(max_workers=TEST_CONCURRENCY) as ex:
        futures = [ex.submit(test_proxy, p) for p in proxies]
        for fut in as_completed(futures):
            proxy, ok = fut.result()
            if ok:
                alive.append(proxy)
            else:
                dead.append(proxy)

    log.info("本地测试完成: 可用 %d 个, 不通 %d 个", len(alive), len(dead))
    return alive


def main():
    log.info("=" * 48)
    log.info("代理抓取启动（proxy-socks5.com + 本地测试）")
    log.info("=" * 48)

    if not PROXY_USER or not PROXY_PASS:
        log.error("❌ 缺少 PROXY_USER 或 PROXY_PASS 环境变量")
        sys.exit(1)

    html = login_and_fetch()
    if not html:
        log.error("❌ proxy-socks5.com 抓取失败")
        sys.exit(1)

    proxies = parse_proxies_from_html(html)
    if not proxies:
        log.warning("⚠️ 未解析到有效代理（可能登录失败或页面结构变化）")
        sys.exit(1)

    proto_count = Counter(p.split("://")[0] for p in proxies)
    log.info("✅ 抓取成功，共 %d 个代理（过滤 x/X 后）", len(proxies))
    for proto, count in proto_count.most_common():
        log.info("  %s: %d 个", proto, count)

    # 本地测试连通性
    alive = test_proxies(proxies)
    if not alive:
        log.error("❌ 所有代理本地测试均不通，退出")
        sys.exit(1)

    # 写入输出文件（仅可用代理）
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for p in alive:
            f.write(p + "\n")

    alive_count = Counter(p.split("://")[0] for p in alive)
    log.info("💾 已写入 %s (%d 个可用代理)", OUTPUT_FILE, len(alive))
    for proto, count in alive_count.most_common():
        log.info("  %s: %d 个", proto, count)


if __name__ == "__main__":
    main()
