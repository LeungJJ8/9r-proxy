# 9R-Proxy — OTC 代理同步工具

从 TG 公开频道 [@otcfxq](https://t.me/otcfxq)（OTC分享群）获取 socks5/http 代理节点，同步到 9router 代理池，测试连通性后输出可用节点列表。

## 工作流程

```
tg-fetch.py ──► socks5.txt ──► proxy-manager.py ──► socks5-otc.txt
     │                                                  │
     └── 提取节点 + 去重                                  └── 可用节点列表
```

## 环境变量

| 变量 | 用途 | 必要 |
|------|------|------|
| `TG_API_ID` | Telegram API ID | 是 |
| `TG_API_HASH` | Telegram API Hash | 是 |
| `TG_SESSION_STR` | Telethon 会话字符串 | 是 |
| `TG_BOT_TOKEN` | TG 通知机器人 Token | 否 |
| `TG_CHAT_ID` | TG 通知接收 Chat ID | 否 |
| `R9_BASE_URL` | 9router 首页地址，https:// 开头 | 是 |
| `R9_PASSWORD` | 9router 登录密码 | 是 |
| `GH_TOKEN` | 写回仓库 Token | 是（CI 用） |
| `TEST_CONCURRENCY` | 并行测试并发数，默认 8 | 否 |
| `TEST_TIMEOUT` | 单节点测试超时秒数，默认 15 | 否 |
| `DEAD_RATIO_LIMIT` | 不通比例阈值，默认 0.9（90%） | 否 |

## 安全机制

每次运行都重新登录获取 `auth_token`，不复用 cookie，避免过期 cookie 导致测试全部 401。

测试环节区分三种结果：

- **连通** → 保留并写入 `socks5-otc.txt`
- **不通**（`ok: false` / 超时 / 连接异常）→ 删除
- **系统性异常**（HTTP 401/403/429/5xx）→ 保留不删除

当「不通 + 异常」比例超过 `DEAD_RATIO_LIMIT`（默认 90%）时，判定为系统性异常（鉴权失效 / 服务故障 / 限流），**跳过全部删除操作，不覆盖 `socks5-otc.txt`**，发送告警通知后以非零码退出。

## 本地运行

```bash
# 安装依赖
pip install -r requirements.txt

# 1. 抓取 TG 节点
export TG_API_ID=xxx TG_API_HASH=xxx TG_SESSION_STR=xxx
python tg-fetch.py

# 2. 同步到 9router
export R9_PASSWORD=xxx
python proxy-manager.py

# 可选：调整并行测试参数
export TEST_CONCURRENCY=16 TEST_TIMEOUT=20
python proxy-manager.py
```

## 定时任务

GitHub Actions 每天早上 **北京时间 18:00** 自动执行：
- 抓取 TG 频道最近 3 天的 socks5 节点
- 同步到 9router 代理池（只增不减）
- 测试连通性，删除不通的节点
- 更新 `socks5-otc.txt` 并提交到仓库
- 发送 TG 通知汇总