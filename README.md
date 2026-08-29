# 9r-Proxy

代理池从 `proxy-socks5.com` 登录抓取 + GitHub free-proxy-list 同步到 9router。

## 功能

1. 登录 `http://proxy-socks5.com/login` 获取代理列表
2. 解析 `socks5://`、`http://`、`https://` 三类代理（过滤掉带 `x` 的掩码 IP）
3. 可选：从 GitHub free-proxy-list 抓取额外代理（`PROXY_SOURCE=github`）
4. 同步到 9router 代理池
5. 全局测试连通性，删除不可用代理
6. Telegram 通知汇总

## GitHub Actions Secrets

| 变量名 | 描述 |
|--------|------|
| `R9_BASE_URL` | 9router 地址（默认 `https://mixed-leah-leung0108-a709260b.koyeb.app`） |
| `R9_PASSWORD` | 9router API 登录密码 |
| `PROXY_USER` | proxy-socks5.com 登录用户名 |
| `PROXY_PASS` | proxy-socks5.com 登录密码 |
| `PROXY_SOURCE` | 代理来源：`socks5-only`（默认）或 `github` |
| `TG_BOT_TOKEN` | Telegram 通知机器人 Token |
| `TG_CHAT_ID` | Telegram 通知接收 Chat ID |

## 本地运行

```bash
pip install -r requirements.txt
python proxy-manager.py
```

环境变量：
- `R9_BASE_URL` — 9router 地址
- `R9_PASSWORD` — 9router 登录密码
- `PROXY_USER` — proxy-socks5.com 用户名（默认 `leung0108`）
- `PROXY_PASS` — proxy-socks5.com 密码（默认 `123456`）
- `PROXY_SOURCE` — 代理来源（默认 `socks5-only`，可选 `github`）
