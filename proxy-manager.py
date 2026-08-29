#!/usr/bin/env python3
"""
9router 代理池同步脚本（GitHub Actions 专用）

直接从 GitHub 下载 socks5-otc.txt 代理列表，同步到 9router：
1. 从 GitHub 下载最新 socks5-otc.txt
2. 用 R9_PASSWORD 登录 9router，获取 auth_token
3. 获取现有代理池，以 ip:port（name）为去重键
4. 只增不减：新增不存在的节点
5. 全局测试连通性，删除测试失败的节点
6. 发送 TG 通知汇总

需要的配置（环境变量）：
  R9_BASE_URL    设为 9router 首页地址
  R9_PASSWORD    9router API 登录密码
  TG_BOT_TOKEN   TG 通知机器人 Token（可选）
  TG_CHAT_ID     TG 通知接收 Chat ID（可选）
"""

import os
import re
import sys
import logging
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

# GitHub 上的 socks5-otc.txt 地址
PROXY_FILE_URL = "https://raw.githubusercontent.com/yutian81/Keepalive/main/9r-proxy/socks5-otc.txt"

BASE_URL = os.getenv("R9_BASE_URL") or "https://9rou.argo.indevs.in"
PASSWORD = os.getenv("R9_PASSWORD") or ""
TYPE_ALLOWED = {"socks5", "http"}    # 只处理这些类型

# 并行测试配置
TEST_CONCURRENCY = int(os.getenv("TEST_CONCURRENCY") or "8")  # 并行线程数
TEST_TIMEOUT = int(os.getenv("TEST_TIMEOUT") or "10")         # 单次测试超时（秒）
DEAD_RATIO_LIMIT = float(os.getenv("DEAD_RATIO_LIMIT") or "0.9")  # 系统异常保护阈值

# 解析节点 URL: scheme://user:pass@ip:port
NODE_RE = re.compile(r"(socks5|http)://[^\s#@]+@(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d+)")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("proxy-manager")


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


def parse_node(url):
    """解析节点 URL，返回 (scheme, ip, port, name) 或 None"""
    m = NODE_RE.match(url)
    if not m:
        return None
    scheme, ip, port = m.group(1), m.group(2), m.group(3)
    return scheme, ip, port, f"{ip}:{port}"


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
    payload = {
        "name": name,
        "proxyUrl": proxy_url,
        "type": "http",
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
    parsed = parse_node(proxy_url)
    return parsed[3] if parsed else proxy_url


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
            f"📥 <b>GitHub 下载</b>：{stats['fetched']} 个节点\n"
            f"🚫 <b>检测到系统性异常</b>：不通比例超过 90%\n"
            f"🛡️ <b>已跳过删除</b>，代理池保持原状\n"
            f"📄 <b>socks5-otc.txt</b> 未更新"
        )
    else:
        message = (
            f"🎉 <b>9Router 代理池更新</b>\n"
            f"----------------\n"
            f"📅 <b>日期</b>：{date_str}\n"
            f"📥 <b>GitHub 下载</b>：{stats['fetched']} 个节点\n"
            f"➕ <b>新增</b>：{stats['added']} 个\n"
            f"❌ <b>删除</b>：{stats['deleted']} 个（测试不通）\n"
            f"✅ <b>最终可用</b>：{stats['total']} 个\n"
            f"📄 <b>socks5-otc.txt</b> 已更新"
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
    log.info("9router 代理池同步启动（GitHub 直连）")
    log.info("目标服务: %s", BASE_URL)

    stats = {"fetched": 0, "added": 0, "deleted": 0, "total": 0, "fail_added": 0, "anomaly": False}

    # 1. 从 GitHub 下载代理列表
    new_proxies = download_proxies()
    if not new_proxies:
        log.error("❌ 未获取到代理节点，退出")
        sys.exit(1)
    stats["fetched"] = len(new_proxies)

    # 解析节点
    new_nodes = []
    for url in new_proxies:
        parsed = parse_node(url)
        if parsed:
            new_nodes.append({"url": url, "scheme": parsed[0], "ip": parsed[1], "port": parsed[2], "name": parsed[3]})
    log.info("解析有效节点: %d 个", len(new_nodes))

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
    for node in new_nodes:
        name = node["name"]
        if name not in existing:
            if api_add_pool(session, name, node["url"]):
                stats["added"] += 1
                log.info("➕ 新增节点: %s", node["url"])
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
