#!/usr/bin/env python3
"""
9router 代理池同步脚本（GitHub Actions 专用）

读取 socks5.txt（tg-fetch.py 产物），同步到 9router：
1. 用 R9_PASSWORD 登录，获取 auth_token（每次运行都重新登录，不复用 cookie）
2. 获取现有代理池，以 ip:port（name）为去重键
3. 只增不减：新增 socks5.txt 中不存在的节点
4. 全局测试连通性，删除测试失败的节点
5. 输出最终可用节点到 socks5-otc.txt（覆盖写）
6. 发送 TG 通知汇总

安全机制：若"不通"比例超过 DEAD_RATIO_LIMIT（默认 90%），判定为系统性异常
（鉴权失效 / 服务故障 / 限流），跳过删除并退出，避免误清空整个代理池。

需要的配置（环境变量）：
  R9_BASE_URL    设为 9router 首页地址
  R9_PASSWORD    9router API 登录密码
  TG_BOT_TOKEN   TG 通知机器人 Token
  TG_CHAT_ID     TG 通知接收 Chat ID
"""

import os
import re
import sys
import logging
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

BASE_URL = os.getenv("R9_BASE_URL") or "https://9rou.argo.indevs.in"
PASSWORD = os.getenv("R9_PASSWORD") or ""
OUTPUT_FILE = "socks5-otc.txt"       # 最终可用节点输出
NODES_FILE = "socks5.txt"            # tg-fetch.py 产物
TYPE_ALLOWED = {"socks5", "http"}    # 只处理这些类型

# 并行测试配置
TEST_CONCURRENCY = int(os.getenv("TEST_CONCURRENCY") or "8")  # 并行线程数
TEST_TIMEOUT = int(os.getenv("TEST_TIMEOUT") or "10")         # 单次测试超时（秒），超时判定为不通
DEAD_RATIO_LIMIT = float(os.getenv("DEAD_RATIO_LIMIT") or "0.9")  # 系统异常保护：不通比例超过此阈值（0~1）时，判定为系统性异常，跳过删除

# 解析节点 URL: scheme://user:pass@ip:port
NODE_RE = re.compile(r"(socks5|http)://[^\s#@]+@(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d+)")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("proxy-manager")


# ================= Session 管理 =================

def make_session() -> requests.Session:
    """构建标准 requests.Session（无预置 cookie，登录后自动携带 auth_token）"""
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


# ================= 9router API =================

def api_login(session: requests.Session) -> bool:
    """登录 9router，返回是否成功（成功时 session 自动保存 auth_token cookie）"""
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


def api_get_pools(session: requests.Session) -> list:
    """获取全部代理池，返回 [{name, proxyUrl, type, id, ...}]（真实响应: {"proxyPools": [...]}）"""
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


def api_add_pool(session: requests.Session, name: str, proxy_url: str) -> bool:
    """新增代理池，返回是否成功（真实响应: HTTP 201 + {"proxyPool": {...}}）"""
    payload = {
        "name": name,
        "proxyUrl": proxy_url,
        "type": "http",  # socks5 节点一律固定为 http
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


def api_test_pool(session: requests.Session, pool_id, timeout: int = TEST_TIMEOUT):
    """测试代理池连通性（真实响应: {"ok": bool, "error": str}）

    返回三态：
      True  - 连通
      False - 不通（含请求超时）
      None  - 系统性异常（鉴权失效 / 服务端 5xx / 限流），不可判定为不通
      timeout 为单次请求超时（秒）"""
    try:
        resp = session.post(f"{BASE_URL}/api/proxy-pools/{pool_id}/test", timeout=timeout)
        if resp.status_code in (401, 403, 429) or resp.status_code >= 500:
            log.error("测试代理池 %s 返回 HTTP %s（系统性异常，不判定为不通）: %s",
                      pool_id, resp.status_code, resp.text[:120])
            return None
        data = resp.json()
        return bool(data.get("ok"))
    except (requests.RequestException, ValueError) as e:
        log.warning("测试代理池 %s 异常（判定为不通）: %s", pool_id, e)
        return False


def api_delete_pool(session: requests.Session, pool_id) -> bool:
    """删除代理池，返回是否成功"""
    try:
        resp = session.delete(f"{BASE_URL}/api/proxy-pools/{pool_id}", timeout=15)
        data = resp.json()
        return bool(data.get("success"))
    except requests.RequestException as e:
        log.error("删除代理池 %s 请求异常: %s", pool_id, e)
        return False


# ================= 工具函数 =================

def parse_node(url: str) -> tuple:
    """解析节点 URL，返回 (scheme, ip, port, name) 或 None"""
    m = NODE_RE.match(url)
    if not m:
        return None
    scheme, ip, port = m.group(1), m.group(2), m.group(3)
    return scheme, ip, port, f"{ip}:{port}"


def is_type_allowed(pool_type: str) -> bool:
    """判断代理池类型是否属于处理范围（socks5/http）"""
    return (pool_type or "").lower() in TYPE_ALLOWED


def read_nodes_file() -> list:
    """读取 socks5.txt，返回解析后的节点 dict 列表"""
    if not os.path.exists(NODES_FILE):
        log.error("未找到 %s，请先运行 tg-fetch.py", NODES_FILE)
        sys.exit(1)
    nodes = []
    with open(NODES_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parsed = parse_node(line)
            if parsed:
                nodes.append({"url": line, "scheme": parsed[0], "ip": parsed[1], "port": parsed[2], "name": parsed[3]})
    return nodes


def extract_name(proxy_url: str) -> str:
    """从 proxyUrl 提取 ip:port 作为 name"""
    parsed = parse_node(proxy_url)
    return parsed[3] if parsed else proxy_url


# ================= 主流程 =================

def main():
    if not PASSWORD:
        log.error("未配置 R9_PASSWORD，脚本退出")
        sys.exit(1)

    log.info("=" * 48)
    log.info("9router 代理池同步启动")
    log.info("目标服务: %s", BASE_URL)

    stats = {"fetched": 0, "added": 0, "deleted": 0, "total": 0, "fail_added": 0, "anomaly": False}

    # 1. 读取 TG 节点
    new_nodes = read_nodes_file()
    stats["fetched"] = len(new_nodes)
    log.info("读取 %s: %d 个节点", NODES_FILE, len(new_nodes))

    # 2. 登录（每次运行都重新登录，不复用 cookie）
    session = make_session()
    if not api_login(session):
        log.error("登录 9router 失败，退出")
        sys.exit(1)
    pools = api_get_pools(session)

    # 3. 构建现有池 {name(ip:port): {id, proxyUrl, type, ...}}，只保留允许类型
    existing = {}
    for p in pools:
        ptype = p.get("type", "")
        if is_type_allowed(ptype):
            name = p.get("name") or extract_name(p.get("proxyUrl", ""))
            existing.setdefault(name, p)
    log.info("现有代理池（允许类型）: %d 个", len(existing))

    # 4. 只增不减：新增 socks5.txt 中不存在的节点
    for node in new_nodes:
        name = node["name"]
        if name not in existing:
            if api_add_pool(session, name, node["url"]):
                stats["added"] += 1
                log.info("新增节点: %s", node["url"])
            else:
                stats["fail_added"] += 1
        # 已存在则跳过（只增不减）

    # 5. 获取最新代理池列表（含新增），全局并行测试连通性
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

    log.info("开始并行测试 %d 个节点（并发 %d，超时 %ds）...",
             len(candidates), TEST_CONCURRENCY, TEST_TIMEOUT)

    live_pools = []
    dead_pools = []
    error_pools = []
    # 每个线程用独立 Session，复用主 session 登录后的 cookie（requests.Session 非线程安全）
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

    # 安全机制：不通比例超过阈值时，判定为系统性异常，跳过删除
    tested = len(candidates)
    dead_ratio = (len(dead_pools) + len(error_pools)) / tested if tested else 0.0
    if error_pools:
        log.error("有 %d 个节点返回系统性异常（鉴权/服务端错误），不计入不通", len(error_pools))
    if tested and dead_ratio > DEAD_RATIO_LIMIT:
        log.error("=" * 48)
        log.error("检测到系统性异常：不通比例 %.1f%%（%d/%d）超过阈值 %.0f%%",
                  dead_ratio * 100, len(dead_pools) + len(error_pools), tested,
                  DEAD_RATIO_LIMIT * 100)
        log.error("疑似鉴权失效 / 服务故障 / 限流，跳过删除以避免误清空代理池")
        log.error("=" * 48)
        stats["anomaly"] = True
        # 异常时不删除、不覆盖 socks5-otc.txt，直接通知后退出
        stats["total"] = len(live_pools)
        try:
            send_tg_notification(stats)
        except Exception as e:
            log.error("发送 TG 通知异常: %s", e)
        sys.exit(1)

    # 删除测试不通的节点（串行，DELETE 很快）
    for pool_id, name in dead_pools:
        log.warning("节点测试不通，删除: %s", name)
        if api_delete_pool(session, pool_id):
            stats["deleted"] += 1
        else:
            log.error("删除节点 %s 失败", name)

    # 系统性异常的节点保留（不删除），但也不写入输出文件
    log.info("测试完成: 存活 %d, 删除 %d, 异常保留 %d",
             len(live_pools), len(dead_pools), len(error_pools))

    # 6. 输出最终可用节点到 socks5-otc.txt（覆盖写）
    stats["total"] = len(live_pools)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for p in live_pools:
            f.write(p.get("proxyUrl", "") + "\n")
    log.info("最终可用节点 %d 个，已写入 %s", stats["total"], OUTPUT_FILE)

    # 7. 发送 TG 通知
    try:
        send_tg_notification(stats)
    except Exception as e:
        log.error("发送 TG 通知异常: %s", e)

    log.info("=" * 48)
    log.info("同步完成: 抓取 %d, 新增 %d, 删除 %d, 最终 %d",
             stats["fetched"], stats["added"], stats["deleted"], stats["total"])


def send_tg_notification(stats: dict):
    """发送 TG 通知汇总"""
    token = os.getenv("TG_BOT_TOKEN") or ""
    chat_id = os.getenv("TG_CHAT_ID") or ""
    if not token or not chat_id:
        log.info("未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，跳过通知")
        return

    bjt = datetime.now(timezone(timedelta(hours=8)))
    date_str = f"{bjt.year}年{bjt.month:02d}月{bjt.day:02d}日"

    if stats.get("anomaly"):
        message = (
            f"⚠️ <b>9Router 代理池异常保护</b>\n"
            f"----------------\n"
            f"📅 <b>日期</b>：{date_str}\n"
            f"📥 <b>TG 抓取</b>：{stats['fetched']} 个节点\n"
            f"🚫 <b>检测到系统性异常</b>：不通比例超过 90%\n"
            f"🛡️ <b>已跳过删除</b>，代理池保持原状\n"
            f"📄 <b>socks5-otc.txt</b> 未更新"
        )
    else:
        message = (
            f"🎉 <b>9Router 代理池更新</b>\n"
            f"----------------\n"
            f"📅 <b>日期</b>：{date_str}\n"
            f"📥 <b>TG 抓取</b>：{stats['fetched']} 个节点\n"
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
            log.info("TG 通知发送成功")
        else:
            log.warning("TG 通知发送失败: %s", result.get("description", "未知错误"))
    except requests.RequestException as e:
        log.error("TG 通知请求异常: %s", e)


if __name__ == "__main__":
    main()
