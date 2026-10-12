# Idea2App

在 `agent/` 引擎之上封装的两阶段流程层。**`agent/` 一个字节不改**,只用它暴露给外部的缝
(`skills.load_skills` / `hooks.PRE_TOOL_USE` / `hooks.STOP` / `context.setup` + `loop.run`)。

## 两阶段

```
IDEA → PRD_DRAFT → PRD_APPROVED → SPEC_DRAFT → SPEC_APPROVED(冻结) → EXECUTING → DONE
            │ 人审①              │ 人审②
        (产品方向)          (技术可执行性)          └─ Phase 2 自驱,越界才问
            └────────── Phase 1 human-in-the-loop ──────────┘
```

- **Phase 1 规划层**:模型写 `PRD.md` / `SPEC.md` 时,文档审批门(`gate.py`)把全文渲染给人,
  `y` 放行并推进阶段、`n` 带理由退回返工。两道关就是两次 `input()`。
- **Phase 2 执行层**:`SPEC.md` 冻结后敲 `/build`,引擎自驱——动作走
  `permission.py`、拆活走 task system / team / workflow、验收走 `goal.py`。全部是引擎已有的能力。

## 运行

```bash
# 任意项目目录(cwd = 项目;PRD.md/SPEC.md/.idea2app 落这里)
python D:\AI\code_agent\Idea2App\main.py

# 会话内
idea2app >> 做个记账 App        # IDEA 阶段直接敲想法 = /new-app
idea2app >> /install-skill prd   # 装技能 (市场名 / 本地目录 / git URL)
idea2app >> /build               # Spec 冻结后开跑 Phase 2
idea2app >> /phase  /reset  /help
```

引擎模型配置沿用 `D:\AI\code_agent\.env`(agent/config.py 读它)。

## 技能库与市场清单

- 用户技能库默认 `D:\AI\Job_Resume\.claude\skills`(可用环境变量 `IDEA2APP_SKILLS_DIR` 改),
  装进来的 skill 落这里、也从这里装载。
- 市场清单是 `market.json`(默认 `skills_dir/market.json`,可用 `IDEA2APP_MARKET` 改),格式见
  [`market.json.example`](market.json.example):`name -> { source, desc }`,`source` 是本地目录或 git URL。
- 安装两次来源:`<市场名>` 查 market.json;`<路径/URL>` 直接解析。校验复用 agent 的
  `skills._parse_skill_md`,坏 skill 拒绝入内;同名覆盖。

## 目录

```
Idea2App/
├─ main.py      宿主:接线 agent 的缝 + 斜杠命令 + 两阶段编排
├─ phase.py     阶段状态机 + 冻结(.idea2app/phase.json 落盘)
├─ gate.py      文档审批门(PreToolUse + Stop 两个 hook)
├─ install.py   skill 安装器(市场清单 + 本地/URL)
├─ settings.py  技能库/市场/项目路径
└─ market.json.example
```

## 测试

```bash
python test/smoke_idea2app.py
```
覆盖:阶段机(推进/落盘/重启续上/非法迁移)、安装器(本地路径/市场名/坏 skill 拒绝)、
文档审批门(否决串、y 推进阶段、非产物不拦)、以及 gate + loop 的集成(不碰真 API)。