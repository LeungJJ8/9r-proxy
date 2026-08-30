# 9r-Proxy

代理池从 `proxy-socks5.com` 登录抓取，同步到 9router。

## 架构：两步分离

| 步骤 | 脚本 | 说明 |
|------|------|------|
| 1 | `fetch_proxies.py` | 登录 `proxy-socks5.com` 抓取代理，过滤 x/X IP，输出 `proxies.txt` |
| 2 | `sync_9router.py` | 读取 `proxies.txt`，同步到 9router，测试连通性，删除不通，TG 通知 |

## 功能

1. 登录 `http://proxy-socks5.com/login` 获取代理列表
2. 解析 `socks5://`、`http://` 代理类型（过滤掉带 `x` 的掩码 IP）
3. 同步到 9router 代理池
4. 全局测试连通性，删除不可用代理
5. Telegram 通知汇总

## GitHub Actions Secrets

| 变量名 | 描述 |
|--------|------|
| `R9_BASE_URL` | 9router 地址 |
| `R9_PASSWORD` | 9router API 登录密码 |
| `PROXY_USER` | proxy-socks5.com 登录用户名 |
| `PROXY_PASS` | proxy-socks5.com 登录密码 |
| `TG_BOT_TOKEN` | Telegram 通知机器人 Token |
| `TG_CHAT_ID` | Telegram 通知接收 Chat ID |

## 本地运行

```bash
pip install -r requirements.txt

# 步骤 1: 抓取代理
PROXY_USER=leung0108 PROXY_PASS=123456 python fetch_proxies.py

# 步骤 2: 同步到 9router
R9_BASE_URL=https://your-r9-url R9_PASSWORD=your_password python sync_9router.py
```

环境变量（步骤 1）：
- `PROXY_USER` — proxy-socks5.com 用户名
- `PROXY_PASS` — proxy-socks5.com 密码

环境变量（步骤 2）：
- `R9_BASE_URL` — 9router 地址
- `R9_PASSWORD` — 9router 登录密码
- `TG_BOT_TOKEN` / `TG_CHAT_ID` — TG 通知（可选）
