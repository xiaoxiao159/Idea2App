"""SSE 事件协议:backend/ 与 frontend/app.js 之间唯一的契约。

传输:GET /api/stream(SSE,server -> client)+ POST /api/message(client -> server)。
每条 SSE 事件是 `data: <json>\n\n`,`{"type": ..., ...}`。

server -> client(type):
  hello         连接就绪,带当前 stage
  phase         stage 变化(= 阶段机推进)
  turn_start    引擎开始跑一轮(前端亮「思考中」)
  tool_call     模型要调一个工具(name + 摘要 arg)
  tool_result   工具执行完(ok + 结果预览)
  gate_request  Phase 1 人审批关卡:PRD/SPEC 全文到了,等浏览器点「通过/驳回」
  doc           产物文档快照(kind=prd|spec, content, status=draft|approved|frozen, version)
  assistant     一轮最终答复文本
  usage         一轮 token 消耗(input/output)
  permission    Phase 2 越界询问(v1 按策略 auto,发此事件只为可见)
  error         异常提示
  turn_end      一轮结束(前端熄灭「思考中」)

client -> server(type):
  user    {text}                新想法 / 对话输入
  verdict {kind, accept, reason} 回应 gate_request
  build   {}                    SPEC 冻结后启动 Phase 2
  reset   {}                    回到 IDEA 并清空前端状态
"""


def ev(t: str, **data) -> dict:
    d = {"type": t}
    d.update(data)
    return d