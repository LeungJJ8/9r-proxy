#!/usr/bin/env python3
"""
9router 代理池同步脚本（GitHub Actions 专用）

从 proxy-socks5.com 登录抓取代理列表，同步到 9router：
1. 登录 proxy-socks5.com 获取代理列表
2. 用 R9_PASSWORD 登录 9router，获取 auth_token
3. 获取现有代理池，以 ip:port（name）为去重键
4. 只增不减：新增不存在的节点
5. 全局测试连通性，删除测试失败的节点
6. 发送 TG 通知汇总

可选：从 GitHub free-proxy-list 抓取额外代理

需要的配置（环境变量）：
  R9_BASE_URL    设为 9router 首页地址
  R9_PASSWORD    9router API 登录密码
  TG_BOT_TOKEN   TG 通知机器人 Token（可选）
  TG_CHAT_ID     TG 通知接收 Chat ID（可选）
  PROXY_USER     proxy-socks5.com 登录用户名
  PROXY_PASS     proxy-socks5.com 登录密码
  PROXY_SOURCE   代理来源：socks5-only 或 github（默认 socks5-only）
"""

import os
import re
import sys
import logging
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from collections import Counter

# proxy-socks5.com 配置
PROXY_SITES_LOGIN_URL = "http://proxy-socks5.com/login"
PROXY_SITES_PROXY_LIST_URL = "http://proxy-socks5.com/proxy_list"
PROXY_USER = os.getenv("PROXY_USER") or "leung0108"
PROXY_PASS = os.getenv("PROXY_PASS") or "123456"

# GitHub free-proxy-list 配置 (databay-labs: ip:port 纯文本，3个文件)
GITHUB_PROXY_LIST_URL = "https://raw.githubusercontent.com/databay-labs/free-proxy-list/master"
GITHUB_PROXY_FILES = ["socks5.txt", "socks4.txt", "http.txt"]
GITHUB_PROXY_SOURCE = os.getenv("PROXY_SOURCE") or "github"  # github 或 socks5-only

# 9router 配置
BASE_URL = os.getenv("R9_BASE_URL") or "https://mixed-leah-leung0108-a709260b.koyeb.app"
PASSWORD = os.getenv("R9_PASSWORD") or ""
TYPE_ALLOWED = {"socks5", "http"}  # 只处理这些类型

# 并行测试配置
TEST_CONCURRENCY = int(os.getenv("TEST_CONCURRENCY") or "8")
TEST_TIMEOUT = int(os.getenv("TEST_TIMEOUT") or "10")
DEAD_RATIO_LIMIT = float(os.getenv("DEAD_RATIO_LIMIT") or "0.9")

# 解析节点 URL: scheme://user:pass@ip:port
# 同时支持 socks4, socks5, http, https
NODE_RE = re.compile(r"(socks4|socks5|http)s?://(?:[^\s#@]+@)?(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d+)")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("proxy-manager")


# ================= Proxy List Fetching =================

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


def parse_proxies_from_text(text):
    """解析纯文本代理列表（GitHub free-proxy-list 格式）"""
    proxies = []
    seen = set()

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        # 匹配 socks5://ip:port 或 http://ip:port 或 https://ip:port
        m = NODE_RE.match(line)
        if m:
            protocol = m.group(1).lower()
            ip = m.group(2)
            port = m.group(3)

            # socks4 统一转为 socks5
            if protocol == "socks4":
                protocol = "socks5"

            # 过滤掉 ip 中包含 x/X 的代理
            if 'x' in ip.lower():
                continue

            # 跳过不支持的类型
            if protocol not in TYPE_ALLOWED:
                continue

            proxy = f"{protocol}://{ip}:{port}"
            if proxy not in seen:
                seen.add(proxy)
                proxies.append(proxy)

    return proxies


def fetch_from_github():
    """从 GitHub databay-labs/free-proxy-list 抓取代理（3个纯文本文件）"""
    import urllib.request
    log.info("📥 正在从 GitHub databay-labs/free-proxy-list 抓取代理列表...")

    all_proxies = []

    try:
        for filename, default_proto in [
            ("socks5.txt", "socks5"),
            ("socks4.txt", "socks4"),
            ("http.txt", "http"),
        ]:
            url = f"{GITHUB_PROXY_LIST_URL}/{filename}"
            log.info("  抓取 %s ...", filename)
            try:
                with urllib.request.urlopen(url, timeout=30) as resp:
                    content = resp.read().decode('utf-8', errors='ignore')

                # 解析 ip:port 格式
                count = 0
                for line in content.splitlines():
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue

                    # ip:port 格式
                    if ':' in line:
                        ip, port = line.split(':', 1)
                        ip = ip.strip()
                        port = port.strip()

                        # 过滤 x/X 掩码 IP
                        if 'x' in ip.lower():
                            continue

                        # socks4 转 socks5
                        proto = default_proto
                        if proto == "socks4":
                            proto = "socks5"

                        # 只保留允许的类型
                        if proto not in TYPE_ALLOWED:
                            continue

                        proxy = f"{proto}://{ip}:{port}"
                        all_proxies.append(proxy)
                        count += 1

                log.info("    %s: %d 个 (%s)", filename, count, default_proto)
            except Exception as e:
                log.warning("    %s 抓取失败: %s", filename, e)

        # 去重
        seen = set()
        proxies = []
        for p in all_proxies:
            if p not in seen:
                seen.add(p)
                proxies.append(p)

        if proxies:
            proto_count = Counter(p.split("://")[0] for p in proxies)
            log.info("✅ GitHub 解析完成，共 %d 个有效代理", len(proxies))
            for proto, count in proto_count.most_common():
                log.info("  %s: %d 个", proto, count)
        return proxies

    except Exception as e:
        log.error("❌ GitHub 抓取失败: %s", e)
        return []


def fetch_proxies():
    """从 proxy-socks5.com 登录抓取所有协议类型的代理"""
    log.info("📥 正在从 proxy-socks5.com 登录抓取代理列表...")
    html = login_and_fetch()
    if not html:
        log.warning("⚠️ proxy-socks5.com 抓取失败")

    proxies = parse_proxies_from_html(html)
    if proxies:
        proto_count = Counter(p.split("://")[0] for p in proxies)
        log.info("✅ 抓取成功，共 %d 个有效代理", len(proxies))
        for proto, count in proto_count.most_common():
            log.info("  %s: %d 个", proto, count)
    else:
        log.warning("⚠️ proxy-socks5.com 无有效代理")

    return proxies


# ================= Session 管理 =================

def make_session():
    """构建标准 requests.Session"""
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


# ================= 9router API =================

def api_login(session):
    """登录 9router"""
    try:
        resp = session.post(f"{BASE_URL}/api/auth/login", json={"password": PASSWORD}, timeout=15)
        data = resp.json()
        if data.get("success"):
            log.info("9router 登录成功")
            return True
        log.error("9router 登录失败: %s", data.get("message", "未知错误"))
        return False
    except requests.RequestException as e:
        log.error("9router 登录请求异常: %s", e)
        return False


def api_get_pools(session):
    """获取全部代理池"""
    try:
        resp = session.get(f"{BASE_URL}/api/proxy-pools", timeout=15)
        data = resp.json()
        pools = data.get("proxyPools") if isinstance(data, dict) else None
        if isinstance(pools, list):
            return pools
        log.warning("获取代理池响应异常: %s", resp.text[:200])
        return []
    except requests.RequestException as e:
        log.error("获取代理池请求异常: %s", e)
        return []
    except ValueError:
        log.error("获取代理池响应非 JSON: %s", resp.text[:200])
        return []


def api_add_pool(session, name, proxy_url):
    """新增代理池"""
    # 解析协议类型
    m = NODE_RE.match(proxy_url)
    pool_type = m.group(1) if m else "socks5"

    payload = {
        "name": name,
        "proxyUrl": proxy_url,
        "type": pool_type,
        "isActive": True,
        "strictProxy": False,
    }
    try:
        resp = session.post(f"{BASE_URL}/api/proxy-pools", json=payload, timeout=15)
        if resp.status_code in (200, 201):
            return True
        log.warning("新增代理池 %s 失败: HTTP %s %s", name, resp.status_code, resp.text[:200])
        return False
    except requests.RequestException as e:
        log.error("新增代理池 %s 请求异常: %s", name, e)
        return False


def api_test_pool(session, pool_id, timeout=TEST_TIMEOUT):
    """测试代理池连通性，返回三态：True/False/None"""
    try:
        resp = session.post(f"{BASE_URL}/api/proxy-pools/{pool_id}/test", timeout=timeout)
        if resp.status_code in (401, 403, 429) or resp.status_code >= 500:
            log.error("测试代理池 %s 返回 HTTP %s（系统性异常）", pool_id, resp.status_code)
            return None
        data = resp.json()
        return bool(data.get("ok"))
    except (requests.RequestException, ValueError) as e:
        log.warning("测试代理池 %s 异常: %s", pool_id, e)
        return False


def api_delete_pool(session, pool_id):
    """删除代理池"""
    try:
        resp = session.delete(f"{BASE_URL}/api/proxy-pools/{pool_id}", timeout=15)
        data = resp.json()
        return bool(data.get("success"))
    except requests.RequestException as e:
        log.error("删除代理池 %s 请求异常: %s", pool_id, e)
        return False


def is_type_allowed(pool_type):
    """判断代理池类型是否属于处理范围"""
    return (pool_type or "").lower() in TYPE_ALLOWED


def extract_name(proxy_url):
    """从 proxyUrl 提取 ip:port 作为 name"""
    m = NODE_RE.match(proxy_url)
    if m:
        return f"{m.group(2)}:{m.group(3)}"
    return proxy_url


def send_tg_notification(stats):
    """发送 TG 通知汇总"""
    token = os.getenv("TG_BOT_TOKEN") or ""
    chat_id = os.getenv("TG_CHAT_ID") or ""
    if not token or not chat_id:
        log.info("ℹ️ 未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，跳过通知")
        return

    bjt = datetime.now(timezone(timedelta(hours=8)))
    date_str = f"{bjt.year}年{bjt.month:02d}月{bjt.day:02d}日"

    if stats.get("anomaly"):
        message = (
            f"⚠️ <b>9Router 代理池异常保护</b>\n"
            f"----------------\n"
            f"📅 <b>日期</b>：{date_str}\n"
            f"📥 <b>抓取节点</b>：{stats['fetched']} 个\n"
            f"🚫 <b>检测到系统性异常</b>：不通比例超过 90%\n"
            f"🛡️ <b>已跳过删除</b>，代理池保持原状\n"
        )
    else:
        message = (
            f"🎉 <b>9Router 代理池更新</b>\n"
            f"----------------\n"
            f"📅 <b>日期</b>：{date_str}\n"
            f"📥 <b>抓取节点</b>：{stats['fetched']} 个\n"
            f"➕ <b>新增</b>：{stats['added']} 个\n"
            f"❌ <b>删除</b>：{stats['deleted']} 个（测试不通）\n"
            f"✅ <b>最终可用</b>：{stats['total']} 个\n"
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
            log.warning("⚠️ TG 通知发送失败: %s", result.get("description", "未知错误"))
    except requests.RequestException as e:
        log.error("❌ TG 通知请求异常: %s", e)


def main():
    log.info("=" * 48)
    log.info("9router 代理池同步启动（主源 proxy-socks5 + GitHub 备用）")
    log.info("目标服务: %s", BASE_URL)

    stats = {"fetched": 0, "added": 0, "deleted": 0, "total": 0, "fail_added": 0, "anomaly": False}

    # 1. 抓取代理列表
    # 主源: proxy-socks5.com
    socks_proxies = fetch_proxies()
    all_proxies = list(socks_proxies)
    log.info("proxy-socks5.com 抓取: %d 个", len(socks_proxies))

    # 备用源: GitHub free-proxy-list（仅当主源失败/为空时启用）
    if len(all_proxies) == 0:
        log.warning("⚠️ proxy-socks5.com 无代理，启用 GitHub free-proxy-list 备用源...")
        github_proxies = fetch_from_github()
        log.info("GitHub 备用源抓取: %d 个", len(github_proxies) if github_proxies else 0)
        all_proxies.extend(github_proxies)
    else:
        log.info("主源正常，跳过 GitHub 备用源")

    # 去重
    new_proxies = list(dict.fromkeys(all_proxies))
    stats["fetched"] = len(new_proxies)
    log.info("合并去重后共 %d 个代理", len(new_proxies))

    if not new_proxies:
        log.error("❌ 未获取到代理节点，退出")
        sys.exit(1)

    # 2. 登录 9router
    session = make_session()
    if not api_login(session):
        log.error("登录 9router 失败，退出")
        sys.exit(1)
    pools = api_get_pools(session)

    # 3. 构建现有池
    existing = {}
    for p in pools:
        ptype = p.get("type", "")
        if is_type_allowed(ptype):
            name = p.get("name") or extract_name(p.get("proxyUrl", ""))
            existing.setdefault(name, p)
    log.info("现有代理池（允许类型）: %d 个", len(existing))

    # 4. 只增不减：新增不存在的节点
    for proxy in new_proxies:
        name = extract_name(proxy)
        if name not in existing:
            if api_add_pool(session, name, proxy):
                stats["added"] += 1
                log.info("➕ 新增节点: %s", proxy)
            else:
                stats["fail_added"] += 1

    # 5. 获取最新列表，全局测试
    pools = api_get_pools(session)
    candidates = []
    for p in pools:
        ptype = p.get("type", "")
        if not is_type_allowed(ptype):
            continue
        pool_id = p.get("id") or p.get("_id")
        if not pool_id:
            continue
        candidates.append((pool_id, p.get("name") or extract_name(p.get("proxyUrl", "")), p))

    log.info("🔍 开始并行测试 %d 个节点（并发 %d，超时 %ds）...",
             len(candidates), TEST_CONCURRENCY, TEST_TIMEOUT)

    live_pools = []
    dead_pools = []
    error_pools = []
    auth_cookies = requests.utils.dict_from_cookiejar(session.cookies)

    def test_one(args):
        pool_id, name, p = args
        s = make_session()
        s.cookies.update(auth_cookies)
        ok = api_test_pool(s, pool_id, timeout=TEST_TIMEOUT)
        return pool_id, name, p, ok

    with ThreadPoolExecutor(max_workers=TEST_CONCURRENCY) as ex:
        futures = [ex.submit(test_one, c) for c in candidates]
        for fut in as_completed(futures):
            pool_id, name, p, ok = fut.result()
            if ok is True:
                live_pools.append(p)
            elif ok is None:
                error_pools.append((pool_id, name))
            else:
                dead_pools.append((pool_id, name))

    # 安全机制
    tested = len(candidates)
    dead_ratio = (len(dead_pools) + len(error_pools)) / tested if tested else 0.0
    if error_pools:
        log.error("⚠️ 有 %d 个节点返回系统性异常", len(error_pools))
    if tested and dead_ratio > DEAD_RATIO_LIMIT:
        log.error("=" * 48)
        log.error("🚫 检测到系统性异常：不通比例 %.1f%% 超过阈值 %.0f%%",
                  dead_ratio * 100, DEAD_RATIO_LIMIT * 100)
        log.error("🛡️ 跳过删除以避免误清空代理池")
        log.error("=" * 48)
        stats["anomaly"] = True
        stats["total"] = len(live_pools)
        try:
            send_tg_notification(stats)
        except Exception as e:
            log.error("发送 TG 通知异常: %s", e)
        sys.exit(1)

    # 删除不通节点
    for pool_id, name in dead_pools:
        log.warning("❌ 节点测试不通，删除: %s", name)
        if api_delete_pool(session, pool_id):
            stats["deleted"] += 1

    log.info("测试完成: 存活 %d, 删除 %d, 异常保留 %d",
             len(live_pools), len(dead_pools), len(error_pools))

    # 6. 发送 TG 通知
    try:
        send_tg_notification(stats)
    except Exception as e:
        log.error("❌ 发送通知异常: %s", e)

    log.info("=" * 48)
    log.info("📊 同步完成: 下载 %d, 新增 %d, 删除 %d, 最终 %d",
             stats["fetched"], stats["added"], stats["deleted"], stats["total"])


if __name__ == "__main__":
    main()