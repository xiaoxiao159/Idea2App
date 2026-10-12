"""workflow (s16) 冒烟:schema 校验 / meta 校验 / 同步桥整跑 / resume / 失败并入结果。"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(tempfile.mkdtemp())

from agent import workflow  # noqa: E402

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok - {name}")


# ---- 1. SimpleJsonSchema:valid / enum 之外 / 缺 required / 类型错 ----
sch = workflow.SimpleJsonSchema(workflow.FINDINGS_SCHEMA)
ok_flag, err = sch.validate({"findings": [{"title": "nt", "severity": "high"}]})
assert ok_flag is True and err is None
ok("schema: 合法 finding 通过")

ok_flag, err = sch.validate({"findings": [{"title": "x", "severity": "critical"}]})
assert ok_flag is False
ok_flag, err = sch.validate({"title": "no findings"})
assert ok_flag is False
ok("schema: enum 之外 / 缺 required 不过")

vsch = workflow.SimpleJsonSchema(workflow.VERDICT_SCHEMA)
ok_flag, _ = vsch.validate({"isReal": True, "reason": "ri"})
assert ok_flag is True
ok_flag, _ = vsch.validate({"isReal": "yes", "reason": "x"})
assert ok_flag is False
ok("schema: 类型错(boolean 收 string)不过")

# ---- 2. validate_meta / check_permission ----
assert workflow.validate_meta(workflow.SAMPLE_META) is workflow.SAMPLE_META
for bad in ({"description": "d"}, {"name": "no slash!", "description": "d"}, "not-a-dict"):
    try:
        workflow.validate_meta(bad)
        raise SystemExit(f"should have raised: {bad}")
    except workflow.WorkflowInputError:
        pass
ok("validate_meta: 缺 name/名字非法/非 dict 全抛")

assert workflow.check_permission(workflow.SAMPLE_META) == "allow"
try:
    workflow.check_permission(workflow.SAMPLE_META, settings={"deny": ["review-changes"]})
    raise SystemExit("should have raised: denied")
except workflow.WorkflowInputError:
    pass
ok("check_permission: 缺省 allow,deny 名单拒绝")

# ---- 3. 整跑(MockAgentRunner 确定性,不打 API)----
out = json.loads(workflow.run_workflow_sync(
    "review-changes", args={"changes": workflow.DEMO_CHANGES}
))
assert out["launched"]["status"] == "async_launched"
assert out["task"]["status"] == "completed"
assert workflow.RUN_ID_RE.fullmatch(out["task"]["runId"])
assert out["task"]["workflowName"] == "review-changes"
assert out["task"]["usage"]["agents"] >= 4
assert isinstance(out["result"]["confirmed"], list)
run_id = out["task"]["runId"]
store = Path(".runtime")
assert (store / f"{run_id}.journal.jsonl").is_file()
assert (store / f"{run_id}.output.json").is_file()
assert (store / f"{run_id}.json").is_file()
ok("run_workflow_sync: 落 journal/output/snapshot,task completed")

# ---- 4. 输入错误 ----
assert workflow.run_workflow_sync("nope").startswith("Error:")
assert workflow.run_workflow_sync("review-changes", args="not-a-dict").startswith("Error:")
ok("run_workflow_sync: 未知 workflow / args 非 dict → Error")

# 脚本内抛错不炸整次调用,归进 result 且 task 标 failed
bad = json.loads(workflow.run_workflow_sync("review-changes", args={"changes": 123}))
assert bad["task"]["status"] == "failed" and "changes" in bad["result"]["error"]
ok("脚本抛错: task.status=failed,错误并入 result 而非上抛")

# ---- 5. resume:同参数续跑 / 参数不一致拒 ----
out2 = json.loads(workflow.run_workflow_sync(
    "review-changes", args={"changes": workflow.DEMO_CHANGES},
    resume_from_run_id=run_id,
))
assert out2["task"]["status"] == "completed" and out2["launched"]["status"] == "async_launched"
ok("resume: 同参数续跑成功")

mismatch = workflow.run_workflow_sync(
    "review-changes", args={"changes": "different code"},
    resume_from_run_id=run_id,
)
assert mismatch.startswith("Error:") and "do not match" in mismatch
ok("resume: 参数不一致拒绝")

print(f"\nALL {PASS} assertions passed")