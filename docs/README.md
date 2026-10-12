# Oracle Agent — Runner 控制平台 (v1)

**一句话**：让 Oracle 通过"**出站拉取**"控制任意 Runner（GitHub Actions / VPS / 裸机），**Runner 永不开公网入站**。

- 协议规范：[docs/oracle-agent-protocol-v1.md](docs/oracle-agent-protocol-v1.md)
- 部署与验证：[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)
- 旧架构参考代码：[docs/reference/proxy-manager/](docs/reference/proxy-manager/)（已下线，仅存档）

---

## 1. 架构总览

```
        GitHub Actions（只做 Bootstrap，之后退出）
                │ workflow_dispatch
                ▼
┌────────────────────────────────────────────┐
│ Runner VM（临时；sleep 99999 常驻，由 Oracle 关机）│
│   ├─ SSH        (runner:runner)            │
│   ├─ shell-mcp  (systemd 常驻)             │
│   └─ Oracle Agent（纯 stdlib，零依赖）        │
└──────────────────────┬─────────────────────┘
                       │  outbound HTTPS（每 30s 拉取）
                       ▼
           oracle-agent.femboy.us.ci          ← Cloudflare 隧道，自动 TLS
                       │  (VPS 侧 cloudflared 下行到本机)
                       ▼
      Oracle VPS  127.0.0.1:8700
      oracle-agent.service（FastAPI + SQLite）
      ├─ /opt/oracle-agent/server/  控制器
      └─ agentctl（manage CLI）
```

**三阶段分工**（与最初设计一致）：

| 阶段 | 职责 | 工具 |
|---|---|---|
| ① Bootstrap | 初始化 SSH、装 MCP、装 Agent | GitHub Actions（一次性） |
| ② 主动轮询 | Agent 出站 pull/heartbeat，Oracle 下发任务 | 长轮询 + 心跳 |
| ③ 能力扩展 | Oracle 决定安装 Cloudflare Connector / Docker 等 | 任务下发（install_* 预留） |

**生命周期终止（GH Runner）**：主路径 = **Oracle 用 PAT 调 GitHub API cancel 该 run**（agent 死透也能终止，VM 由 GitHub 可靠回收）；agent 侧 `shutdown` 任务保留，供 VPS/裸机等无 GitHub 控制的节点使用。

---

## 2. 目录结构

```
oracleagent/
├── server/                   Oracle 控制器（FastAPI + sqlite3，仅依赖 fastapi/uvicorn）
│   ├── main.py               入口（uvicorn server.main:app）
│   ├── api/agents.py         register / heartbeat / report
│   ├── api/tasks.py          pull（长轮询）/ result
│   ├── db.py                 SQLite 持久化（agents / tasks 两表）
│   ├── ctl.py                管理 CLI（agentctl）
│   └── models.py / auth.py   Pydantic 模型 / Bearer 校验
├── agent/                    Runner Agent 客户端（纯 Python 标准库，零第三方依赖）
│   ├── agent.py              register → heartbeat/pull → execute → report 主循环
│   ├── config.json           oracle_url / agent_name / capabilities / 频率参数
│   ├── handlers/             任务类型处理器（test / exec / shutdown）
│   ├── install.sh            systemd 安装脚本（支持 ORACLE_URL / AGENT_NAME 覆盖）
│   └── oracle-agent.service  systemd 单元
└── docs/
    ├── oracle-agent-protocol-v1.md   协议规范（唯一权威）
    ├── DEPLOYMENT.md                 部署 + 验证记录
    └── reference/proxy-manager/      旧架构存档（已下线）
```

---

## 3. 通信机制与频率

Runner 只出站。每个 Runner 的真实流量：

| 请求 | 频率 | 说明 |
|---|---|---|
| `POST /pull` | **每 30s 一次**（`pull_timeout_sec`） | 长轮询，65s 上限；有任务立即返回，无任务挂满 30s 后返回 `null` |
| `POST /heartbeat` | **每 60s 一次**（`heartbeat_interval_sec`） | 更新 `last_seen`；Server 超过 180s 未见心跳标记 offline |
| `POST /register` | 进程启动时 1 次 | 带 token 重注册（幂等） |
| `POST /result` | 每个任务后 1 次 | 回报 status/output |
| `POST /report` | 事件时 | 如 `tunnel_ready` |

**任务拾取延迟**：最差 ≤ 30s（一个 pull 周期），通常在秒级（pull 已在悬挂时任务立即返回）。
参数可调：`pull_timeout_sec` 调小 → 更快但请求更频繁；`heartbeat_interval_sec` 调小 → 离线判定更快。

---

## 4. 快速上手（本地联调）

```bash
# 1) Server（本仓库）
python3 -m venv .venv && .venv/bin/pip install fastapi uvicorn
.venv/bin/python -m uvicorn server.main:app --host 127.0.0.1 --port 8700

# 2) Agent（另开终端）
cd agent && python3 agent.py          # 自动 register，写 state.json(600)

# 3) Oracle 侧下发任务
.venv/bin/python -m server.ctl list
.venv/bin/python -m server.ctl task push <agent_id> test '{"hi":"there"}'
.venv/bin/python -m server.ctl task push <agent_id> exec '{"command":["echo","hello"],"timeout_sec":30}'
```

---

## 5. 已部署环境（生产）

| 项 | 值 |
|---|---|
| Oracle 控制器 | `oracle-agent.service` @ VPS `127.0.0.1:8700`（root 目录 `/opt/oracle-agent`，专用用户 `oracle-agent`） |
| 公网入口 | `https://oracle-agent.femboy.us.ci`（Cloudflare 隧道 HTTP 公网主机名 → `localhost:8700`；VCN 无需开端口） |
| Bootstrap 仓库 | `sourspicysoup/oracle-agent-bootstrap`（private；workflow `Oracle Runner Bootstrap`） |
| 数据库 | `/opt/oracle-agent/server/data/oracle.db`（SQLite，agents/tasks） |

**部署/重建/验证细节见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。**

---

## 6. 安全说明

- **出站唯模型**：Runner 无公网入站；拉取与心跳全部经 Cloudflare 隧道（自动 TLS）。
- **认证**：每 Agent 独立 Bearer token；Server 只存 sha256 哈希；`agent_id` 已存在时 register 需带原 token（防冒领）。
- **Runner 风险**：GitHub 托管 VM 短暂开放 SSH（`runner:runner`）、公网 IP 会出现在 Actions 日志——仅用于自有 Runner，且生命周期由 Oracle `shutdown` 结束。
- **最小权限**：Bootstrap 仓库只放初始化脚本，业务/隧道逻辑一律由 Oracle 任务下发（见协议附录 B）。

---

## 7. 状态与路线

- **协议 v1 已稳定**：`register / heartbeat / pull / result / report` 五个接口。
- **任务类型**：`test`、`exec`、`shutdown` 已实现；`install_cloudflare`、`configure_tunnel`、`install_mcp`、`install_docker`、`configure_ssh`、`start_service`/`stop_service` 预留（协议 §6.2）。
- **已验证**：VPS 本地闭环、公网隧道闭环、真实 GitHub Actions Runner 全链路（agent `shutdown` 与 GitHub cancel 两种终止方式均验证）——记录见 DEPLOYMENT.md。
- **待办候选**：
  - **Agent Stream（统一反向控制通道）**：协议已定（协议文档附录 C v1.2）；一条出站连接承载 Control Plane（exec/shutdown/install…）+ MCP Plane（mcp 只是其一），待实现 Agent 侧 Stream 主循环 + `mcp_adapter.py` + Oracle 侧 MCP Gateway；Runner 不再需要 Cloudflare Tunnel
  - agentctl 封装（`ssh <runner>` 等）
  - Cloudflare Access Service Token 加固
  - `ack` 租约语义细化