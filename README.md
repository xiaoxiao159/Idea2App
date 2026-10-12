<div align="center">

# ✦ Idea2App

**从零手写的多智能体 AI 编程引擎 · 「一句话想法 → 可运行 App」两阶段流程**

一个想法进来，先经过两道**人工审批门**（产品方向、技术可执行性），冻结 Spec 后交给多智能体团队**自驱开发**，全程在一个浏览器工作台里可观测、可干预。

<br/>

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![DeepSeek](https://img.shields.io/badge/LLM-DeepSeek-4D6BFE?style=for-the-badge&logo=deepseek&logoColor=white)](https://www.deepseek.com/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![WebSocket](https://img.shields.io/badge/Transport-WebSocket-4E9A06?style=for-the-badge&logo=socket.io&logoColor=white)]()
[![License](https://img.shields.io/badge/License-TODO-lightgrey?style=for-the-badge)]()

</div>

> **设计哲学**：规划层让人来拍板，执行层让引擎自驱。人只守两道关（产品方向 + 技术可执行性），剩下的活全部交给引擎已有的能力——task system、team、workflow、goal——去拆、去跑、去验收。**越界才问，其余自驱。**

---

## ✨ 特性

| Feature | Description |
|---|---|
| 🧠 多协议模型适配 | Anthropic 兼容 / OpenAI 兼容双协议 adapter，DeepSeek 实测跑通；接新厂商只需登记一行 + 一个 adapter |
| 🔁 REPL 引擎 | 从零实现的回合循环 + Context 窗口管理（归档 / 压缩 / 仪表 / 挑选），长会话不爆窗 |
| 🛠 工具系统 | `bash`、文件读写、`todo` 清单、`task` 任务系统（依赖图）、`cron` 定时、`skill`、`mcp`、`compact` |
| 👥 多智能体协作 | `spawn_teammate` 队友、`subagent`、`workflow` 编排——并行拆活、计划审批门、事件回流 |
| 🪝 Hook 体系 | `audit` / `permission` / `plan` / 文档审批门，全部可注入、可观测 |
| 🎯 完成条件闸门 | `goal`：给会话设一个完成条件，达标才放行 |
| 🧠 记忆系统 | `facts` 事实沉淀、`sessions` 会话续聊、`AGENTS.md` 约定注入 |
| 📄 两阶段流程层 | `IDEA → PRD(人审) → SPEC(人审冻结) → 自驱开发`，阶段机落盘、重启续上 |
| 📦 Skill 市场 | 技能安装器 + 市场清单，本地目录 / git URL / 市场名三来源，坏 skill 拒绝入内 |
| 🖥 Web 工作台 | supervisor + 每项目独立 worker 子进程，Chat / PRD / SPEC 三视图，实时活动流 + Token 图表 |

---

## 🗺 工作流

```
IDEA → PRD_DRAFT → PRD_APPROVED → SPEC_DRAFT → SPEC_APPROVED(冻结) → EXECUTING → DONE
          │ 人审①              │ 人审②
       (产品方向)          (技术可执行性)          └─ Phase 2 自驱，越界才问
          └────────── Phase 1 · human-in-the-loop ──────────┘
```

- **Phase 1 规划层**：模型写 `PRD.md` / `SPEC.md` 时，文档审批门把全文渲染给人，`y` 放行推进阶段、`n` 带理由退回返工。
- **Phase 2 执行层**：`SPEC.md` 冻结后敲 `/build`，引擎自驱——动作走 `permission`、拆活走 task system / team / workflow、验收走 `goal`。

### 架构分层

```
frontend/ ── 浏览器工作台（原生 HTML/CSS/JS，WebSocket 直连）
backend/  ── supervisor（多项目编排）+ worker 子进程（每项目一进程隔离）+ 注册表/配置存储
Idea2App/ ── 两阶段流程层：阶段机、文档审批门、Skill 安装器（复用引擎，一个字节不改 agent/）
agent/    ── 多智能体引擎：loop / context / tools / hooks / team / workflow / goal / memory / cron / mcp
```

> 核心约束：**`Idea2App/` 不改 `agent/` 一个字节**，只走引擎暴露的对外接口（`skills.load_skills` / `hooks.*` / `context.setup` + `loop.run`）。

---

## 🚀 快速开始

### 1. 环境与依赖

```bash
# Python 3.10+，克隆后装两处依赖
cd d:/AI/code_agent
pip install -r requirements.txt          # 引擎：anthropic / openai / dotenv / yaml
pip install -r backend/requirements.txt  # Web：fastapi / uvicorn / httpx
```

### 2. 配置模型

复制 `.env`（已存在则直接改），填入你的模型凭据：

```bash
# Anthropic 兼容协议（示例：DeepSeek 网关）
ANTHROPIC_API_KEY=sk-你的Key
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
MODEL_ID=deepseek-v4-flash
```

### 3. 三种入口

```bash
# ① 终端 · 两阶段工作流（cwd = 项目目录，PRD/SPEC 落这里）
python Idea2App/main.py

# ② Web · 多项目工作台（浏览器 http://localhost:8000）
python -m backend.supervisor

# ③ Web · 单机模式（自动开浏览器）
python -m backend.server
```

> 终端工作流常用命令：直接敲一句想法（等价 `/new-app`）、`/build` 进入自驱、`/install-skill <名|路径|URL>` 装技能、`/phase` 看阶段、`/reset` 回起点。

<details>
<summary>📋 完整配置项（环境变量）</summary>

| 变量 | 说明 | 默认 |
|---|---|---|
| `MODEL_PROVIDER` | `anthropic`（需 `ANTHROPIC_API_KEY` + `ANTHROPIC_BASE_URL`）或 `openai`（仅需 `OPENAI_API_KEY`） | `anthropic` |
| `MODEL_ID` | 模型名（必填） | — |
| `CONTEXT_WINDOW` | 上下文上限 | `200000` / `128000`（随 provider） |
| `MAX_TOKENS` | 单次输出上限 | `8000` |
| `IDEA2APP_PORT` | Web 端口 | `8000` |
| `IDEA2APP_PERMISSION` | Web 权限模式 | — |
| `IDEA2APP_SKILLS_DIR` | 用户技能库目录 | 见 [Idea2App/settings.py](Idea2App/settings.py) |
| `IDEA2APP_MARKET` | 市场清单 `market.json` 路径 | `skills_dir/market.json` |

</details>

---

## 📁 目录结构

```
code_agent/
├── agent/                 # 多智能体引擎（核心，从零手写）
│   ├── main.py            #   裸引擎 REPL 入口
│   ├── config.py          #   模型配置（多协议登记表）
│   ├── loop.py            #   回合循环
│   ├── context/           #   上下文窗口：归档 / 压缩 / 仪表 / 挑选
│   ├── models/            #   anthropic / openai 协议 adapter
│   ├── tools/             #   bash / files / todo / task / cron / team / workflow / mcp / skill
│   ├── hooks/             #   audit / permission / plan
│   ├── team.py            #   队友协作（spawn_teammate / send_message）
│   ├── subagent.py        #   子智能体
│   ├── workflow.py        #   子步骤编排
│   ├── goal.py            #   完成条件闸门
│   ├── skills.py          #   技能装载 / 解析
│   ├── mcp.py             #   外部工具接入
│   ├── memory/            #   facts / sessions / AGENTS.md
│   └── tasks/             #   任务系统存储
├── Idea2App/              # 两阶段流程层（复用引擎）
│   ├── main.py            #   终端宿主
│   ├── phase.py           #   阶段状态机（.idea2app/phase.json 落盘）
│   ├── gate.py            #   文档审批门
│   ├── install.py         #   Skill 安装器
│   ├── bootstrap.py       #   终端 / Web 共用接线层
│   └── settings.py        #   技能库 / 市场 / 项目路径
├── backend/               # Web 服务层
│   ├── supervisor.py      #   多项目编排 + WS 代理
│   ├── worker.py          #   每项目独立子进程
│   ├── project_server.py  #   单项目 WS 服务
│   ├── registry.py        #   配置 / 项目注册表
│   └── bridge.py          #   Web ↔ 引擎事件桥
├── frontend/              # Web 工作台（原生 HTML/CSS/JS）
├── test/                  # 每模块冒烟测试（smoke_*.py）
├── learn/                 # 学习资料（s01–s17 弧线笔记 / 模块总览）
├── requirements.txt       # 引擎依赖
└── .env                   # 模型凭据（勿提交）
```

---

## 🧪 测试

冒烟测试按模块隔离，纯脚本、不依赖 pytest，逐个直跑：

```bash
python test/smoke_idea2app.py      # 流程层：阶段机 / 安装器 / 审批门 / 集成
python test/smoke_team.py          # 队友协作
python test/smoke_workflow.py      # 编排
python test/smoke_server.py        # Web 单机（TestClient 驱动 WS）
python test/smoke_supervisor.py    # 多项目 supervisor
# 其余：smoke_context / smoke_goal / smoke_hooks / smoke_cron / smoke_mcp
#       smoke_memory / smoke_models / smoke_skills / smoke_subagent / smoke_tasks / smoke_todo / smoke_ws …
```

---

## 🗓 路线图

- [x] s01–s09　引擎地基：REPL 循环、工具、Context 窗口、记忆
- [x] s10–s13　多智能体弧线：team / workflow / subagent
- [x] s14–s17　两阶段流程、Web 工作台、Goal 闸门
- [ ] 接入更多模型厂商 / 协议 `[TODO]`
- [ ] 补齐 License 与贡献指南 `[TODO]`

---

## 📖 参考与学习

本仓库是 s01–s17 学习弧线的产物，配套笔记在 [learn/](learn/)（`s10-s17-overview.html` 为模块总览，图全部内联 SVG、离线自包含）。相关参考实现见 `learn-claude-code`。

<div align="center">

**让想法自己长成应用。** ✦

</div>
