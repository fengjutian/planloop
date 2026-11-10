# 架构与数据契约

## 一、主链路

```
                 ┌────────────┐
 用户问题 ──────► │  spec.py   │  目标 + 验收标准 + 约束 + 预算 + 未知项
                 └─────┬──────┘
                       │ 没有 success_criteria → 抛 SpecError，转提问
                       ▼
                 ┌────────────┐
                 │ decomp.py  │  模板或规划模型 → TaskNode DAG
                 └─────┬──────┘
                       ▼
                 ┌────────────┐
                 │ compile.py │  纯代码校验 + 注入记忆护栏
                 └─────┬──────┘
                       │ 有 error 级问题 → 拒绝执行（不花一分 token）
                       ▼
        ┌──────────► ┌────────────┐
        │            │scheduler.py│  就绪队列 → 执行 → 验证
        │            └─────┬──────┘
        │                  ▼
        │            ┌────────────┐
        │            │ execute.py │  单节点 ReAct 内环（4 种动作）
        │            └─────┬──────┘
        │                  ▼
        │            ┌────────────┐
        │            │ verify.py  │  L1 确定性 / L2 评委 / L3 人工
        │            └─────┬──────┘
        │                  │ 失败
        └──── replan.py ◄──┘  分类 → 重试 / 修复 / 提问 / 停止
```

## 二、阶段契约

| 阶段 | 输入 | 输出 | 失败语义 |
|---|---|---|---|
| intake | JSON | `ProblemSpec` | `SpecError`（缺验收、种类非法、阻塞未知项） |
| decompose | spec + model | `Plan{nodes}` | `PlanError`（JSON 不可解析、节点缺字段） |
| compile | plan | `CompileReport` | `ok=False` → 运行被拒绝 |
| execute | node | `NodeOutcome` | `status=failed` + 失败签名 |
| verify | criteria | `VerificationReport` | 每条 criterion 一个 `Verdict` |
| replan | 失败报告 | `Decision` | `stop` → `BlockedReport` |

## 三、关键不变量

1. **没有验收标准不进规划。** `ProblemSpec.require_ready()` 在调度器入口强制。
2. **执行者不能给自己发合格证。** `Verifier.assert_independent()` 检查模型实例，相同即抛错。
3. **不可逆节点必须有人的验收项。** `compile` 的 `E_NO_GATE`。
4. **所有循环都有次数上限。** 步数、尝试、子树重规划、全局重规划、token。
5. **状态只写事件日志。** 任何阶段都不允许"只存在于内存里的结论"。

## 四、数据契约

### 节点（`decomp.TaskNode`）

```json
{
  "id": "T2",
  "goal": "修复 total() 的折扣计算",
  "deps": ["T1"],
  "inputs":  {"api": "pricing.total(v2)"},
  "outputs": {"files": "shop/pricing.py"},
  "acceptance": [
    {"id": "A1", "kind": "command", "spec": "python -m unittest discover -s tests -q", "must": "exit=0"}
  ],
  "est_cost": 0.4,
  "risk": "medium",
  "reversible": true
}
```

四要素缺一不可：**输入契约、输出契约、验收条件、成本估计**。

### 验收项（`spec.Criterion`）

| kind | 层 | 语义 | `must` 语法 |
|---|---|---|---|
| `command` | L1 | 跑命令 | `exit=0`、`stdout~=正则`、`contains=文本` |
| `file_exists` | L1 | 文件存在 | — |
| `file_contains` | L1 | 文件包含文本 | 期望子串 |
| `json_has_keys` | L1 | JSON 顶层键 | `a,b,c` |
| `rubric` | L2 | 模型评审 | 必须回引原文，否则判失败 |
| `human` | L3 | 人工闸门 | 默认拒绝 |

### 事件日志（`blackboard`，JSONL，append-only）

```json
{"seq":12,"ts":1731,"kind":"action","node_id":"T1","data":{"action":"run","detail":"exit_code=1..."}}
{"seq":13,"ts":1731,"kind":"failure","node_id":"T1","data":{"reason":"e019a37b: ModuleNotFoundError: ..."}}
{"seq":14,"ts":1732,"kind":"replan","node_id":"T1","data":{"klass":"execution","action":"retry","reason":"..."}}
{"seq":15,"ts":1733,"kind":"lesson_learned","node_id":"T1","data":{"id":"e019a37b","strength":"guard"}}
```

事件类型：`plan / compile / node_start / action / failure / memory_hit / guard_catch / replan / artifact / lesson_learned / node_end / run_end`。

**设计约束**：日志是唯一的事实来源，任何"报告"都是它的投影。回放与审计都基于它。

## 五、失败决策表（`replan.py`）

| 失败类型 | 判定信号 | 动作 | 上限 |
|---|---|---|---|
| execution | L1 命令失败、JSON 非法 | 换思路重试 | 每节点 2 次 |
| plan | 依赖缺失、接口不匹配 | 局部修复 | 每子树 3 次 |
| assumption | rubric 失败 | 回 probe | 由全局上限约束 |
| spec_gap | 无法判定、人工拒绝 | **停下提问** | 立即 |
| environment | 超时、文件消失 | 重新观测后重试 | 1 次 |
| budget | 记账抛异常 | 停止并报告 | 立即 |

## 六、上下文切片

每个节点拿到的上下文由 `Blackboard.context_for()` 构造，上限约 1400 字符：

```
# Global goal              全局目标（一行）
# Current node T2          当前节点目标 + 契约 + 验收
# Upstream results         直接依赖的产物摘要（不是全文）
# Your previous failures   本节点自己的失败史（最近 3 条）
# Known dead ends          已经确认走不通的路
# Project facts            与当前节点关键词匹配的事实
# Recent observations      最近 2 次工具输出（截断）
```

这是长任务稳定性的第一杠杆：**上下文长度是效果的一阶杀手**，而不是一个可以靠"更大窗口"解决的问题。

## 七、持久化与续跑（P0）

```
<workspace>/.planloop/runs/<run_id>/
    state.json      spec、plan、节点状态与尝试次数、产物摘要、预算用量、重规划计数
    events.jsonl    append-only 事件日志（与 Blackboard 共用）
```

`run_id` 形如 `r-20261109-2215-fix-the-discount`：时间戳 + 目标摘要，人能看懂。

**写入是原子的**（临时文件 + rename）：崩溃后要么是旧状态、要么是新状态，不会出现半截 JSON。

**续跑语义**：

| 状态 | 续跑时 |
|---|---|
| `done` | 跳过，产物摘要从 state 恢复进 blackboard |
| `pending` | 重新执行 |
| `running`（崩溃时正在跑） | 视为未完成，重新执行 |
| 预算用量 | 从 state 恢复，不会因为重启而"重置额度" |
| 重规划计数 | 从 state 恢复，防止靠重启绕过熔断 |

```bash
planloop run spec.json -w ./repo          # 自动落盘到 .planloop/runs/<run_id>/
planloop runs list -w ./repo              # 看所有运行及进度
planloop run --resume -w ./repo           # 续跑最近一次
planloop run --resume r-20261109-2215-... -w ./repo
```

**一个刻意设计**：`running` 状态在续跑时被当作未完成。因为我们无法知道崩溃时那一步到底做完了没有，**宁可重做一次，也不要相信半成品**。

## 八、上下文压缩（P2）

上下文切片有硬预算（默认 1400 字符）。超预算时按两级处理：

| 级别 | 手段 | 何时用 |
|---|---|---|
| 1 | 模型摘要（`task=compress`） | 段落足够长（≥700 字符）且有预算 |
| 2 | 抽取式兜底 | 无模型、模型失败、或预算太小 |

抽取式兜底不调用模型，**保留带信号的行走**：错误/异常、文件路径、命令、`exit_code`，按分数排序后保留，其余折叠成 `... [n lines elided] ...`。

```python
from planloop.compress import Compressor
text = bb.context_for(..., max_chars=900, compressor=Compressor(model))
assert len(text) <= 900
```

度量：`Compressor.stats` 记录 `calls / tokens / chars_in / chars_out / ratio / fallbacks`。
**压缩比不是目标，信息保留才是**——一条被压掉的 `ModuleNotFoundError` 可能让下一个节点重走一遍弯路。

## 九、事实自动回写

节点成功后会做一次确定性抽取，把可验证的观测变成记忆（**不依赖模型主动写**）：

| 抽取 | 证据 | 上限 |
|---|---|---|
| 第一条通过的测试命令 → `fact:test_command` | 重跑一次确认 `exit=0` | 每 run 1 条 |
| 本节点写出的文件 → `fact:file:<path>` | 文件存在性检查 | 每 run 2 条 |

写不进去（证据不成立）就不写。这保证了记忆里不会出现"我们以为跑过、其实没跑通"的命令。

## 十、里程碑层（P3）

节点级验收只回答"这一步做对了吗"，回答不了"合起来还对吗"。里程碑补上这一层：

```json
{
  "milestones": [
    {"id": "M1", "goal": "全量测试仍然通过",
     "acceptance": [{"id": "MA1", "kind": "command",
                     "spec": "python -m unittest discover -s tests -q", "must": "exit=0"}]}
  ],
  "nodes": [
    {"id": "T1", "milestone": "M1", "goal": "加 mul()",
     "acceptance": [{"id": "A1", "kind": "command", "spec": "...test_mul.py...", "must": "exit=0"}]},
    {"id": "T2", "milestone": "M1", "goal": "给 add() 加 docstring",
     "acceptance": [{"id": "A2", "kind": "file_contains", "spec": "calc.py", "must": "\"\"\""}]}
  ]
}
```

触发时机：**某个里程碑的所有节点都完成后**，在挑选下一个节点之前检查。

```
节点各自验收通过  →  里程碑全量验收  →  人工闸门  →  独立 commit(planloop: M1)
                              ↓ 失败
                        blocked: milestone M1 failed，附真实测试输出
```

三条硬规则：

1. **人工闸门默认拒绝。** 不传 `--auto-approve` 也不传 `milestone_gate` 时，里程碑停在 `M1:blocked` 等人看——长任务的方向错误只能由人纠正。
2. **失败不自动重试。** 节点级失败可重试；里程碑失败通常意味着"计划或理解错了"，自动重试只会烧更多预算。
3. **里程碑通过有自己的提交点**，所以回退粒度是"节点 / 里程碑 / 全局"三档。

实测（`examples/milestone_demo.py`）：

```
  node T1: done (3 steps)      # 自己的定向测试通过
  node T2: done (2 steps)      # 自己的 file_contains 通过
milestones: M1:failed
blocked: milestone M1 failed end-to-end verification
  needed: [FAIL] L1 MA1: exit=1 (want 0)
          FAIL: test_add ... AssertionError: 0 != 4
```

**两个节点都"通过"了，产品依然是坏的**——这就是里程碑存在的全部理由。
