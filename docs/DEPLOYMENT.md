# Oracle Agent 部署文档（v1）

> 原文档随架构演进多次修订；本版为最终稳态：GitHub Bootstrap + Oracle 控制器（VPS）+ Runner Agent（出站拉取）。

## 1. 部署环境

- **主机**：Oracle Cloud VPS `vnic-1`（Ubuntu 24.04 aarch64，公网 IP `213.35.122.16`）
- **访问**：SSH `HuaweiAgent@213.35.122.16:22`（备用通道：MCP 隧道 `127.0.0.1:13005`）
- **控制器**：`/opt/oracle-agent/`，专用用户 `oracle-agent`，systemd 单元 `oracle-agent.service`
- **监听**：`0.0.0.0:8700`（公网直连入口 `http://213.35.122.16:8700`，iptables 已放行 `dport 8700` 并持久化到 `rules.v4`；CF 隧道入口 `https://oracle-agent.femboy.us.ci` 同时可用）

## 2. 网络拓扑（生产）

```
Runner ──outbound HTTPS──► https://oracle-agent.femboy.us.ci
                                  │ Cloudflare 隧道（自动 TLS）
                                  ▼
                        VPS cloudflared ──► http://127.0.0.1:8700
                                            oracle-agent.service
```

- 隧道：token 隧道（ID `f316b3f1-8693-4e0c-b03e-3af886e4c50b`，Zero Trust 远程托管）
- 公网主机名：`oracle-agent.femboy.us.ci → http://localhost:8700`（面板配置，云端自动下发）

## 3. 已部署组件

| 组件 | 位置 (VPS) | 说明 |
|---|---|---|
| Oracle 控制器 (Server) | `/opt/oracle-agent/server/` | FastAPI，`oracle-agent.service` 常驻 |
| Python 环境 | `/opt/oracle-agent/venv/` | fastapi + uvicorn |
| 数据库 | `/opt/oracle-agent/server/data/oracle.db` | SQLite，agents/tasks 两表 |
| 管理 CLI | `/opt/oracle-agent/venv/bin/python -m server.ctl` | 以 `oracle-agent` 用户运行 |

## 4. 远程管理命令

```bash
# 服务状态 / 日志
systemctl status oracle-agent
journalctl -u oracle-agent -f

# 查看注册表/任务
sudo -u oracle-agent bash -c 'cd /opt/oracle-agent && venv/bin/python -m server.ctl list'

# 下发任务（test / exec / shutdown / 预留类型）
sudo -u oracle-agent bash -c 'cd /opt/oracle-agent && venv/bin/python -m server.ctl task push <agent_id> test "{\"hi\":\"there\"}"'
sudo -u oracle-agent bash -c 'cd /opt/oracle-agent && venv/bin/python -m server.ctl task push <agent_id> exec "{\"command\":[\"echo\",\"hello\"],\"timeout_sec\":30}"'
sudo -u oracle-agent bash -c 'cd /opt/oracle-agent && venv/bin/python -m server.ctl task push <agent_id> shutdown "{}"'

# 任务状态
sudo -u oracle-agent bash -c 'cd /opt/oracle-agent && venv/bin/python -m server.ctl task status <task_id>'
```

## 5. 从零部署清单（若需重建）

```bash
# 1. 代码 + venv
sudo mkdir -p /opt/oracle-agent
sudo tar xzf oracle-agent-server.tar.gz -C /opt/oracle-agent   # server/ 目录
sudo python3 -m venv /opt/oracle-agent/venv
sudo /opt/oracle-agent/venv/bin/pip install fastapi uvicorn

# 2. 专用用户
sudo useradd --system --home /opt/oracle-agent --shell /usr/sbin/nologin oracle-agent
sudo chown -R oracle-agent:oracle-agent /opt/oracle-agent

# 3. systemd 单元（仓库 server 侧同款；绑定 127.0.0.1:8700）
sudo cp oracle-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now oracle-agent

# 4. 验证
curl -s http://127.0.0.1:8700/healthz        # {"ok":true}

# 5. 隧道（Cloudflare Zero Trust 面板）
#    Public Hostname: oracle-agent.femboy.us.ci → http://localhost:8700
```

## 6. 管理查询（Swagger / Admin API）

- Swagger UI：`https://oracle-agent.femboy.us.ci/docs`；OpenAPI：`/openapi.json`
- 只读查询端点（admin 标签），需 `Authorization: Bearer <ORACLE_ADMIN_TOKEN>`
- 服务端配置：`/etc/default/oracle-agent`（root 600）——`ORACLE_ADMIN_TOKEN`、`GH_OWNER/GH_REPO/GH_PAT`（供 `admin/github/workflows` 查 GitHub 正在跑的 workflow）
- **端点分层**：
  - **公开端点**（Swagger 可见，无需认证）：`/exec`、`/exec-many`、`/exec/interactive`、`/agents`、`/active`、`/runs`、`/tasks` 等——访客可调用 exec 执行简单命令
  - **秘密 URL 端点**（Swagger 隐藏，token 走路径，复用 `LOG_VIEW_TOKEN`）：`/trigger/{token}`、`/cancel/{token}`、`/template-view/{token}`、`/logs/{token}`——仅持有 token 的管理员可调用
  - `ADMIN_AUTH_ENABLED` 环境变量仍控制非 exec 类公开端点的 Bearer 认证开关（当前 `false`）
- **触发 run**：`POST /api/v1/admin/trigger/{LOG_VIEW_TOKEN}` `{"count": N}`（默认 1，上限 20）
- **停止 run**：`POST /api/v1/admin/cancel/{LOG_VIEW_TOKEN}` `{"run_id":N}` / `{"run_ids":[...]}` / `{"all_in_progress":true}`（GitHub cancel）
- **在 run 上执行命令**：`POST /api/v1/admin/exec` 与 `POST /api/v1/admin/exec-many`（`run_ids` / `vm_ids` / `agent_ids` / `all_online`）；结果经 `GET /api/v1/admin/tasks/{task_id}` 轮询
  - **标识层级**（四选一、互斥）：`run_id`（GitHub，首选）→ `id`（内部兼容）→ `agent_id`（Agent 直接，校验在线）→ `vm_id`（Azure VM 反查）；响应回显 `resolved_by`
  - Run 属目标走状态机（queued/provisioning→409 agent_not_ready；timeout→409 agent_not_available；终态→409 run_not_active；running→下发）
- **Run 生命周期**：runs 表升级为 GitHub Run 生命周期记录（`trigger` 瞬间即建档）
  - Oracle 状态：`queued`（GitHub 已接受、Job 未开始）→ `provisioning`（Job 已开、Agent 未注册）→ `running`（Agent 已注册）→ `completed / failed / cancelled`（按 GitHub conclusion 归一化）；`provisioning_timeout`（5 分钟无 Agent）
  - 双层原始字段：`github_status` / `github_conclusion` 与归一化 `status` 并存，不丢 GitHub 原始语义
  - `exec` 按状态拒绝：queued/provisioning → `409 agent_not_ready`；provisioning_timeout → `409 agent_not_available`；终态 → `409 run_not_active`；404 仅表示"Oracle 从未记录过该 run"
  - 同步：查询 runs/active 时惰性拉 GitHub 状态回写（无需独立同步服务）
- **访问日志**：`/opt/oracle-agent/server/data/access.log`（JSON lines；完整 headers + body + 时间，敏感头打码；RotatingFileHandler 10MB×5 轮转；默认跳过 agent 协议请求）
- **日志查询专用端点**：`GET /api/v1/admin/logs/<LOG_VIEW_TOKEN>`（**Swagger 隐藏**；`?lines/q/path/since/file`；token 走路径，独立于 ADMIN_AUTH_ENABLED；未配 `LOG_VIEW_TOKEN` 则 404；自身访问在日志里路径打码为 `<token>`）
- 本地查看 token：`sudo grep ORACLE_ADMIN_TOKEN /etc/default/oracle-agent`
- 部署说明：`server/api/admin.py` 挂载于 `main.py`；`docs/sample-imds-gh-runner.json` 为真实 GH Runner 的 IMDS 样本

## 7. Runner 生命周期终止（推荐）

**GitHub-hosted Runner 主路径**：Oracle 用 PAT 调 GitHub API 取消 run，不依赖 agent 是否存活，VM 由 GitHub 回收：

```bash
curl -sS -X POST -H "Authorization: Bearer $PAT" -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/<owner>/<repo>/actions/runs/<run_id>/cancel
```

agent 侧 `shutdown` 任务（`{"delay_sec": 60}`，先回报再断电）保留，用于 VPS/裸机等无 GitHub 控制的节点。

## 6. 验证记录（2026-09-23）

### 6.1 VPS 本地闭环

| 任务 | 状态 | 结果 |
|---|---|---|
| test | success | `{"echo": {...}, "message": "hello from runner"}` |
| exec echo | success | stdout `hello from runner`, exit_code 0 |
| exec exit 1 | failed | stdout `oops`, exit_code 1 |
| exec sleep 5 (limit 2s) | timeout | error `command timed out after 2s` |

### 6.2 公网隧道闭环（agent 指向 `https://oracle-agent.femboy.us.ci`）

| 任务 | 状态 | 结果 |
|---|---|---|
| test | success | `hello from runner`（经 CF 隧道） |
| exec echo | success | stdout `hello via CF tunnel`, exit_code 0 |

> 坑位记录：Cloudflare 默认拦截 `Python-urllib/*` User-Agent（error 1010）；agent 已改为自定义 UA `oracle-agent/0.1.0`。

### 6.3 GitHub Actions 全链路（真实 Runner）

仓库：`sourspicysoup/oracle-agent-bootstrap`（private），workflow `Oracle Runner Bootstrap`，run #35849192311

| 环节 | 结果 |
|---|---|
| 触发 workflow → 新建 GH 托管 VM | ✅ runner 上线 |
| VM 上 SSH（`runner:runner` + sshd）/ shell-mcp | ✅（后续步骤能跑即证明） |
| 安装 Oracle Agent → 注册 | ✅ `runner-runnervmlun5p` online |
| Oracle 下发 `test` / `exec` | ✅ success（拿到 runner 公网 IP `172.182.211.21`） |
| Oracle 下发 `shutdown` | ✅ VM 断电 → GH job `completed(failure)`（预期） |

**结论**：GitHub 只做 Bootstrap；Runner 出站 HTTPS 经 CF 隧道连 Oracle；任务下发与生命周期（关机）由 Oracle 掌握——与设计一致。

## 7. 已完成清理项

- ✅ 旧架构 `proxy-manager`（8000 端口）删除；参考代码存档 `docs/reference/proxy-manager/`
- ✅ PostgreSQL `mailrelay` 库删除
- ✅ 测试 agent 目录与临时文件清理
- ✅ 旧 token 误建的空仓库 `femboyenjoy/oracle-agent-bootstrap` 删除

## 8. 待办候选

- **Agent Stream（统一反向控制通道）**：协议已定（协议附录 C v1.2）；实现 stream 主循环 + `mcp_adapter.py` + MCP Gateway，exec/mcp/shutdown 等全走 Runner 单条出站连接
- Cloudflare Access Service Token 加固（Runner 端带 header）
- 协议 `ack` 租约语义、重投递上限（见协议 §5.2）
- 新任务类型：`install_cloudflare` / `configure_tunnel` / `install_mcp` / `install_docker` 等（协议 §6.2）
### 6.4 修复后全链路（2026-09-23，agent 加 Azure 身份 + 网络异常兜底）

- Azure 身份：`azure_vm_id=e3af8cdf-91e7-42a1-ba1a-6e4ef49e0ebc`，agent_id=`runner-runnervmtr4k5-e3af8cdf`（= hostname + vmId 前 8 位），register meta 上报 vm_id / vm_name
- 5 个 workflow 步骤全绿；test/exec 成功；shell-mcp active
- 稳定性：agent 持续心跳 5+ 分钟无 crash（修复 TimeoutError 崩溃循环后）
- 生命周期：Oracle 下发 `shutdown` → success（shutdown-issued, delay 60s）→ VM 断电 → GH job `completed(failure)`（预期）
- 结论：**GH Runner 终止主路径建议用 GitHub cancel**（PAT，agent 无关）；agent `shutdown` 供非 GH 节点
