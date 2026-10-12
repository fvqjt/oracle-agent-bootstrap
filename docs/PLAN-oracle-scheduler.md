# 计划：Oracle Runner 调度与部署系统（Task Template + 多 GitHub 账号 + Supervisor 常驻守护）

> 状态：**计划落盘；Phase 1 已完成；Phase 4A 实施依据（2026-09）**
> 本文档为**实现基线**——Part E 核心语义（E1–E5）已定稿固定，后续实现不得再改。

## 〇 设计总纲（Why）

**职责迁移**：GitHub 从"业务自动化平台"降级为"临时 Runner 提供器"；Oracle Agent 接管原先 GitHub Workflow 的业务编排职责。

| 能力 | GitHub Workflow | Oracle Agent Template |
|---|---|---|
| 创建临时 Runner / Ubuntu 环境 | ✅ | ❌ |
| Bootstrap SSH / MCP / Agent | ✅ | ❌ |
| 业务安装/配置/Secret/步骤顺序/超时/重试/状态/日志 | ❌ | ✅ |

- `task_templates` + `template_secrets` 本质是 **Oracle 自己的 Workflow/Job Engine**（Template Run + Step 状态机），不是普通 exec 队列。
- Template 建立在 **Agent Stream 的 exec operation 之上**，不需要新的 Agent 协议：
  `Template Engine → Control Plane exec → Agent Stream → Runner → result → 下一步`
- MVP（Phase 2A）：ordered steps `{command, timeout, env_refs, result}`；以后逐步加 retry/condition/on_failure/outputs/depends_on/health_check。

**四层**：
```
Oracle Controller
   ├─ Supervisor        (WHEN / WHO)
   ├─ Template Engine   (WHAT)  → Template Run → Step1 → Step2 …
   └─ Account Manager   (WHICH PAT)
              ↓
          GitHub Run → Runner Agent → Agent Stream → exec/mcp/shutdown
```

> 本文档合并了此前两份草案（multi-PAT / Task Template）并吸收最新调整：
> - Workflow 只负责"把 Runner 拉起来"；业务部署全部移交 Oracle。
> - Template = 部署任务（named steps 线性依赖），Secret 独立存储只留引用。
> - 不依赖"多个 exec 天然 FIFO"（Agent Stream 未来支持并发 multiplex），模板显式顺序。
> - 与多账号（Credential Profile）在 `/admin/deploy` 组合。

---

## Part A：GitHub Account / Credential Profile（多账号扩容）

### A1 概念分层

```
GitHub Account (Credential Profile)
      │  PAT
      ▼
GitHub Workflow
      │  Run
      ▼
Oracle Run
      │  Agent
      ▼
Runner
```

- PAT 不与 Runner/Agent 绑定；Agent 完全不知道 PAT/GitHub 用户/repo，协议零变化。
- 多 profile 可同 owner 不同 PAT（primary / primary-2 / secondary）。

### A2 数据模型

```sql
CREATE TABLE github_accounts (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  name                TEXT UNIQUE,      -- primary / secondary / primary-2
  owner               TEXT,
  repo                TEXT,
  credential          TEXT,             -- PAT，仅服务端私用
  enabled             INTEGER DEFAULT 1,
  max_concurrent_runs INTEGER DEFAULT 20,   -- 配置化，不写死
  created_at          TEXT, updated_at TEXT
);
```

runs 表加 `account_id`；现有行回填默认 profile。

### A3 API（Phase 2）

| 端点 | 改动 |
|---|---|
| `POST /admin/trigger` | 加可选 `account` |
| `GET /admin/github/workflows` | 加可选 `account` |
| `POST /admin/cancel` | 按 `run.account_id` 自动反查 PAT，调用者零账号信息 |
| `GET /admin/accounts`（新） | in_progress / max_concurrent_runs / 余量 |

调度规则仅：`当前 in_progress < max_concurrent_runs` → 可分发。

### A4 不对外开放 / 路径隐藏（2026-09 定）

- **多账号模块 API 不对外开放**：账号管理（github_accounts 增删改、`/admin/accounts` 并发视图）一律 `include_in_schema=False`，**不出现在 Swagger**，仅服务端内部 / agentctl 使用。
- 公开端点（`trigger` / `cancel` / `github/workflows`）只加**可选 `account` 参数**（不算新模块，对外无新增管理路径）。
- cancel 按 `runs.account_id` 自动反查 PAT——调用者不需要知道账号，自然无需暴露账号管理。

---



## Part B：Task Template（部署任务模板）

### B1 定位

```
GitHub Workflow（最小）
    ├── SSH           ← 排障备用入口
    ├── shell-MCP     ← Runner 基础能力
    ├── Oracle Agent  ← 控制入口
    └── sleep 99999   ← 维持 Job
```

Xray / cloudflared / atlas / Telegram Bot **全部移出 Workflow**。Workflow 不需要知道 atlas1 是什么。

### B2 数据模型：步骤与 Secret 分离

```sql
CREATE TABLE task_templates (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT UNIQUE,        -- atlas1 / atlas2 / ...
  description TEXT,
  steps       TEXT,               -- JSON：named steps，线性依赖
  enabled     INTEGER DEFAULT 1,
  created_at  TEXT, updated_at TEXT
);

CREATE TABLE template_secrets (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  template_id INTEGER REFERENCES task_templates(id),
  key         TEXT,               -- CF_TUNNEL_TOKEN / BOT_TOKEN / CHAT_IDS
  value       TEXT                -- 真正 secret 单独存储
);
```

- Template 只持有 **secret references**（key 名）；值在 `template_secrets`。
- `GET /admin/templates/{name}` 只返回 `"secrets": ["CF_TUNNEL_TOKEN","BOT_TOKEN","CHAT_IDS"]`，**永不返回值**。

### B3 Steps 语义（关键调整）

```json
{
  "name": "atlas1",
  "steps": [
    { "name": "install_xray",       "command": ["bash","-c","..."], "timeout_sec": 120 },
    { "name": "validate_xray",      "command": ["xray","-test","-c","/etc/xray/config.json"] },
    { "name": "start_xray",         "command": ["bash","-c","systemctl enable --now xray"] },
    { "name": "install_cloudflared", "command": ["bash","-c","cloudflared service install"], "env_refs": ["CF_TUNNEL_TOKEN"] },
    { "name": "install_atlas",      "command": ["bash","-c","..."] },
    { "name": "install_tgbot",      "command": ["bash","-c","..."],  "env_refs": ["BOT_TOKEN","CHAT_IDS"] }
  ]
}
```

- **模板步骤显式顺序依赖**：Step N+1 只在 Step N `success` 后下发；失败即标记 template run 失败。
  - 不依赖"多个 exec 自然 FIFO"——Agent Stream 未来支持 request A/B/C 并发 multiplex，顺序不再可靠。
- 演进目标：Template Run / Task Run（独立执行实体，含步骤级状态、结果、重试）。
- 实现提示（后续）：Template Run 执行器在 result 回调触发下一步，可后台线程/惰性驱动。

### B4 Secret 注入（沿用 exec env 透传设计）

- Agent exec handler 支持 `env` 参数 → `subprocess.run(env={**os.environ, **env})`（小改动，向后兼容）。
- Token 经 env 注入、不进命令字符串；访问日志对 `env` 值整体打码。

### B5 atlas1 拆分（去掉 SSH/MCP——Bootstrap 已含）

```
atlas1
├── 01 install_xray        mkdir /var/log/v2ray; 下载 Xray; 装 /usr/local/bin/xray; 配置 /etc/xray; 装 xray.service
├── 02 validate_xray       xray -test
├── 03 start_xray          daemon-reload; enable; start
├── 04 install_cloudflared 下载 cloudflared; 装 /usr/local/bin; service install（CF_TUNNEL_TOKEN 来自 secrets）
├── 05 install_atlas       下载 atlas.service; 装 service; enable/start
└── 06 install_tgbot       下载 logsender.py + tgbot.service; 注入 BOT_TOKEN/CHAT_IDS; enable/start
```

### B6 分层（与现有 Agent 架构吻合）

```
              GitHub
                │
          Bootstrap Runner
                ▼
        ┌─────────────────┐
        │      Agent      │
        └────────┬────────┘
                 │  Agent Stream
                 ▼
        ┌─────────────────┐
        │ Oracle Controller│
        └────────┬────────┘
       ┌─────────┴─────────┐
   Control Plane       Template Plane
   exec/shutdown       atlas1 / atlas2
   install/service
```

- Template 最终通过 **Control Plane 的 exec** 执行，**不新建 Agent 协议**。

### B7 模板不在公开 API（2026-09 定）

- **API 不做 templates 模块**：移除原计划的 `/admin/templates` CRUD 公开端点。
- `task_templates` / `template_secrets` 表由**服务端配置 / 本地 CLI（agentctl）直接维护**（或独立配置文件）。
- 执行入口（apply）同样**隐藏**：`include_in_schema=False`（不出现在 Swagger），或仅 agentctl / 服务端内部调用；模板与 secret 值不进公网 API。

### B8 Template 格式 v1（Oracle-native YAML，2026-09 定稿）

> 原则：**格式像 GitHub Actions，语义不要照搬 GitHub Actions**。
> 目标是把现有 workflow 业务步骤"搬"过来，而不是重新实现 GitHub Actions。

#### 格式

```yaml
name: atlas1
description: Deploy atlas1 runner

steps:
  - name: install_xray
    run:
      - bash
      - -c
      - |
        mkdir -p /var/log/v2ray
        curl -fsSL "https://example.com/xray" -o /usr/local/bin/xray
        chmod +x /usr/local/bin/xray

  - name: validate_xray
    run: [xray, -test, -c, /etc/xray/config.json]
    timeout_sec: 120

  - name: start_xray
    run: [systemctl, enable, --now, xray]

  - name: install_cloudflared
    env:
      CF_TUNNEL_TOKEN: { secret: CF_TUNNEL_TOKEN }
    run:
      - bash
      - -c
      - |
        curl -fsSL https://example.com/cloudflared -o /usr/local/bin/cloudflared
        chmod +x /usr/local/bin/cloudflared
        cloudflared service install "$CF_TUNNEL_TOKEN"

  - name: install_atlas
    run:
      - bash
      - -c
      - |
        # install atlas
        ...

  - name: install_tgbot
    env:
      BOT_TOKEN: { secret: BOT_TOKEN }
      CHAT_IDS:  { secret: CHAT_IDS }
    run:
      - bash
      - -c
      - |
        # install telegram bot
        ...
```

#### 字段（第一版）

| 字段 | 版本 | 说明 |
|---|---|---|
| `name` / `description` | ✅ | 元信息 |
| `steps[]` | ✅ | **默认串行**：`steps[0] success → steps[1] → …`；失败即 Template failed |
| `steps[].name` | ✅ | 步骤名（状态/日志用） |
| `steps[].run` | ✅ | **argv 数组**（不是 shell 字符串），最终 `subprocess.run(argv)`；复杂脚本用 `[bash, -c, script]` |
| `steps[].timeout_sec` | ✅ | 超时 |
| `steps[].env` | ✅ | 环境变量；值为 `{ secret: KEY }` 引用 Oracle secret store |
| `steps[].retry` | 预留 | `{max_attempts: N}`（第一版可不实现字段） |
| `needs` / `if` / `matrix` / `jobs` / `permissions` / `runs-on` | ❌ | GitHub Actions 专属，**不引入** |

#### 关键语义

1. **run 保留 argv**：`xray -test -c /etc/xray/config.json` 直接对应 `subprocess.run([...])`，不重新引入 `Oracle → shell string → shell parsing`。
2. **Secret 不仿 `secrets.X`**：用 `env: { KEY: { secret: KEY } }`；Template YAML 只含 **secret reference**，值在 Oracle secret store，**永不进 YAML/API/日志**。
3. **Step 串行**：第一版不做 needs/DAG；需要时以后扩展 `needs: [build]`。
4. **不发明类型**：`install` / `service` / `download` 都是 **exec**；一切最终都是 exec + env。
5. **与 GitHub 的区别**：只借鉴 YAML 表达方式，不背 GitHub Actions 的全部模型。

#### Secret 用例备注（2026-09）

- Secret store 支持任意多个 key；同一隧道 token 若**既用于 service install 又嵌入 service 文件**（如 atlas1.service），建模为独立 secret 并在安装步骤 **sed 注入**（`sed -i "s|<旧值>|$VAR|"`），远程文件可留占位符。
- 旋转/故障切换 = 在 Oracle 改 secret 值即可，仓库与 workflow 零改动；配合多 PAT（Part A）账号/隧道任一失效都能续。

#### 处理管线

```
Template Step
    ↓ resolve secrets（Oracle secret store）
    ↓ construct exec request（argv + env + timeout）
    ↓ Agent Stream (operation=exec)
    ↓ result
    ↓ success → next step；failure → Template failed
```
---

## Part C：组合编排（Phase 2）

```
POST /admin/deploy
{
  "account": "secondary",
  "template": "atlas1"
}
```

```
account secondary → PAT → trigger workflow → Run queued → provisioning
→ Agent registered → Template atlas1 → Step1 → Step2 → ... → deployed
```

- 与 multi-PAT 组合：GitHub 账号决定"在哪开 VM"，Template 决定"装什么"。
- GitHub 越来越像临时 VM 提供器，业务部署逻辑归 Oracle。

---

## Part E：Supervisor（常驻任务守护 / Keep-alive 调度）

> 需求来源：原实现用 **Cloudflare Worker（PAT + 定时触发器）** 每分钟检查 atlas1 是否在跑。
> 迁入 Oracle 后：不用敲 CF Worker 代码，多 PAT 持续续命，状态可审计。
> **设计目标（2026-09 定稿）**：多 PAT 的价值不只是“20 → 40 → 60 并发”，还包括 **Supervisor 的持续运行能力**——
> 某个 GitHub Account 失效时 Oracle 自动切换其它 Credential Profile，不依赖 CF Worker 保存 PAT/cron。

> 🔒 **Part E 核心定义已定稿固定**（commit 3e803f58）：实现时不应再改变 E1–E5 的五条核心语义（最终模型 / 巡检决策 / 多 PAT 分级 / Supervisor≠Template / 全部隐藏）。

### E1 最终模型

```
Oracle Controller
│
├── GitHub Accounts (Credential Profiles)
│     ├── primary
│     ├── secondary
│     └── ...
│
└── Supervisor: atlas1
      ├── repo        gaylish/codespace
      ├── workflow    atlas1.yml
      ├── interval    60s
      ├── min_running 1
      ├── renew_after 5h55m (21300s)
      └── accounts    [primary, secondary, ...]
```

### E2 巡检决策（每 60s，固定语义）

```
查询指定 repo/workflow 的 in_progress runs
        │
  ┌─────┴─────────┐
  无可用 Run    有运行中 Run
  │              │
  ▼              ▼
立即触发     检查【最新】Run
            （最近触发的在跑 Run）
              │
        ┌─────┴─────┐
        <5h55m   >=5h55m
        │         │
        等待      再触发一个
```

- **一轮最多补一个**：单个 supervisor 单次巡检最多 trigger 一个 run（避免并发风暴；下次巡检再评估）。
- **语义**：不是“存在超龄 Run 就只保留一个”，而是：
  - 当前**没有**可用 Run → **补 1 个**
  - **最新 Run（最近触发的在跑 Run）已运行满 5h55m** → **再补 1 个**（滚动补台，旧 Run 继续跑至 GitHub 6h 上限自然回收）
  - 判断基准是**最新 Run**（而不是最旧）：保证"始终存在一个启动时间不早于 5h55m 前 的 Run"。
- 滚动效果（按最新 Run 满 5h55m 补台，规避 6h job 上限前的空窗）：
  ```
  0:00  Run A        （最新=A）
  5:55  Run B        （最新 A 满 5h55m → 补 B；此时 A 仍在跑）
  11:50 Run C        （最新 B 满 5h55m → 补 C）
  17:45 Run D        （最新 C 满 5h55m → 补 D）
  ...
  ```
  - 旧 Run 由 GitHub 6h 上限自然回收；判断始终以“最新”为准。

### E3 多 PAT 故障转移（固定语义）

```
Supervisor.accounts = [primary, secondary, ...]
  primary:  trigger 成功 → 使用
            失败     → cooldown，换下一个
  secondary: trigger 成功 → 使用
             失败     → cooldown
  ...
```

**不要把偶然一次网络错误永久判定 PAT 失效**，按错误类型分级：

| GitHub API 结果 | 判定 | 动作 |
|---|---|---|
| 成功 | 正常 | 使用该 profile |
| 认证失败 / token 无效（401） | **account unhealthy** | 标记并换下一 profile |
| 权限不足（403） | **account unhealthy** | 标记并换下一 profile |
| rate limit（429） | **cooldown** | 冷却等待 reset，换下一 profile；reset 后恢复 |
| 网络超时 / 5xx | **临时失败** | 稍后重试，**不算失效** |

- cooldown/unhealthy 状态按 profile 记录（配置/内存可持久化），不写死“一次失败=永久禁用”。
- 与 Part A（multi-PAT）关系：Supervisor 是指定的故障转移消费者之一。

### E4 Supervisor 与 Template 的关系（不是同一概念）

```
Supervisor = 决定“什么时候创建 Runner、用哪个 GitHub Account”   （WHEN / WHICH）
Template   = 决定“Runner 上线后安装/执行什么”                    （WHAT）
```

```
Supervisor atlas1
    │ trigger
    ▼
GitHub Run
    ▼
Agent register
    ▼
Template atlas1
    ├── Xray
    ├── cloudflared
    ├── atlas
    └── tgbot
```

- 允许：Supervisor A → template atlas1；Supervisor B → template xyz；Supervisor C → **不自动部署 template**。
- Template 沿用 Part B（服务端维护、不公开 API）。
- 先有 bootstrap 基础能力（SSH/MCP/Agent），Template 只负责业务布置。

### E5 可见性（与既定设计一致）

| 模块 | Swagger |
|---|---|
| templates | ❌ 不出现 |
| accounts | ❌ 不出现 |
| supervisors | ❌ 不出现 |

- 全部仅 Oracle 本地 agentctl / 内部配置管理逻辑可操作。
- supervisor 巡检/触发/换台/健康状态记录可审计（本地查看）。

### E6 API 节流

- 每 60s × N supervisor 的 GitHub 查询需合并/限频，避免并发触发 rate limit（配合 E3 的 429 cooldown）。

### E7 实现顺序（定稿，严格按序落）

> 边界：**Supervisor 决定“什么时候/用哪个账号 trigger”；Template 决定“Agent 上线后装什么”**——
> 不要把 Supervisor 写成 Template 的一种特殊形式。两条线保持分离，后续在 Agent 注册事件上组合。

1. **Supervisor 配置模型**：`name / repo / workflow_file / accounts / check_interval_sec / min_running / renew_after_sec / template(可选)`。
2. **GitHub Account / Credential Profile**：PAT 不进入公开 API；profile 健康状态 `healthy / cooldown / unhealthy`；429 按 GitHub `reset` 时间恢复；网络错误临时失败、**不永久封禁**。
3. **Supervisor 巡检循环**：每 `check_interval_sec` 查询 in_progress；无 Run → 补 1；**最新 Run** ≥ `renew_after_sec` → 补 1；**一轮最多补一个**。
4. **Trigger → Run 建档**：`trigger → GitHub run_id → runs(queued) → provisioning → Agent register → running`（复用现有生命周期）。
5. **Template 自动 Apply（后置）**：Agent register 事件 → supervisor/template 关联 → Template Run → step 1 → step 2 → step 3 …（Phase 2A 能力接入）。
6. **可观测性（最后做）**：最近巡检时间、最近 trigger、使用了哪个 account、account cooldown/unhealthy 原因、当前运行中的 Run、最近一次补台原因。

**落地方案**：Phase 4A 先做 **Supervisor 最小闭环**（1→2→3→4，不同时碰 Template）——验证：
`每分钟检查 → GitHub trigger → Run 建档 → Agent 注册 → 5h55m 后补下一台 → 多 PAT 故障切换`
随后 Phase 4B 再接 `template: atlas1` 的 Agent 注册自动部署。

## Part D：实施顺序 / 非目标

### Phase 划分

- ✅ Phase 1（已完成）：Run→Agent 定位（run_id/id/vm_id/agent_id + 状态机 + resolved_by）。
- ⏳ Phase 2A：Task Template（表 + **本地 CLI/服务端维护（无公开 CRUD）** + apply 隐藏端点/agentctl + exec env 透传 + 日志打码）。
- ⏳ Phase 2B：多账号（github_accounts + runs.account_id + 按账号路由 + **模块 API 隐藏、不对外开放**）。
- ⏳ Phase 3：组合编排 `/admin/deploy` + Template Run 增强（重试/步骤状态/告警）。
- ⏳ Phase 4A：**Supervisor 最小闭环**（配置模型 → profile 健康分级 → 巡检循环 → Trigger→Run 建档；不含 Template）。
- ⏳ Phase 4B：**Template 自动 Apply 接入**（Agent 注册 → supervisor/template 关联 → Template Run）。
- ⏳ Phase 5：**可观测性**（巡检/trigger/account/补台原因/当前 Run 视图）。

### 非目标 / 约束

- 不改 agent 协议；Template 经 Control Plane exec 执行。
- Secret 永不通过 API 返回；只在服务端存储与注入；日志打码。
- Workflow 保持"最小 Bootstrap"，业务零渗透。

## 关联现有代码

- 凭据：`server/api/admin.py` `_gh_creds_or_raise()`（单账号，Phase 2B 改按 profile）
- 生命周期：`server/db.py` runs 表 + `create_run_provisioning / sync_run_from_github`
- 执行：`server/api/admin.py` `_resolve_exec_target`（Phase 2A 复用：模板步骤经它定位 run/agent）
- Agent：`agent/handlers/exec.py` 加 `env` 透传（小改）