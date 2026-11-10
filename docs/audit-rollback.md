# 审计与回退

> 状态（2026-11）：
> **已实现** —— git 提交点、失败回滚、stash 保留证据（`workspace.py`）；审计报告（`audit.py`，每次运行自动写 `AUDIT.md`）；按文件/行溯源（`blame.py`，`planloop why`）。
> **未实现** —— 计划版本化（replan 前后的 diff）。
> 下文保留完整设计，实现状态在每节开头标注。

## 〇、审计报告（已实现）

`planloop audit [-w repo] [run-id] [--git] [--out FILE]`，或在每次 `run` 结束时自动写入 `<run_dir>/AUDIT.md`。

报告**不调用模型**——用模型写摘要，等于让被审计方自己写结论。

内容：

| 小节 | 回答什么 | 数据来源 |
|---|---|---|
| 进度 | 每个节点到什么状态、尝试几次、有无提交点 | `state.json` |
| 缺口 | 哪些没做完、哪些靠多次重试才通过、失败产物在哪 | `state.json` + git stash |
| 时间线 | 发生了什么：开始/失败/重规划/回滚/提交 | `events.jsonl` |
| 证据等级 | 这个"完成"有多硬 | 验收项的 kind |
| 变更 / stash | 改了哪些文件、失败产物在哪 | git |

实测输出：

```markdown
## 进度
| 节点 | 状态 | 尝试 | 提交点 | 证据 | 目标 |
| T1 | done | 1 | 364fbc81a0 | B | 修复折扣计算 |
| T2 | failed | 1 | — | A | 新增 export_csv 并让全量测试通过 |

## 缺口
- T2 未完成（failed）: 新增 export_csv 并让全量测试通过
- 1 份失败产物保存在 stash（可审计、可恢复）

## 时间线
- 09:12:23 T2 失败: e150f1c19cdd0: exit=2 (want 0)
- 09:12:24 T2 回滚到 364fbc81a0（保留 stash）
- 09:12:24 T2 重规划 → stop（execution）: execution failure persisted over 1 attempts
```

**证据等级取"最弱环节"**：`command` → A、文件/JSON → B、rubric → C、human/自述 → D。
只统计已完成节点；整体若为 C/D，报告会显式警告"结论不可复现"。

## 一、"任务错了"是四种错

| 类型 | 例子 | 谁发现 | 回退半径 | 处置 |
|---|---|---|---|---|
| 规格错 | 说"优化性能"，其实要降内存 | 只能靠人 | 全局 | 停下提问，不回滚，先改规格 |
| 假设错 | 以为是 Postgres，其实是 MySQL | probe / 报错 | 下游全部 | 回退到 probe 点，重规划下游 |
| 计划错 | 漏了"迁移数据" | 集成测试、接口对不上 | 子树 | 回退到子树起点，局部重分解 |
| 执行错 | 函数改错 | 单测 | 单节点 | 重试或回滚该节点 |

规律：**发现得越晚，回滚半径越大**。所以要么早发现（里程碑人审），要么把回退粒度做细（每节点一个提交点）。两者互补。

### 溯源：`planloop why <path>[:line]`（已实现）

```bash
$ planloop why a.txt -w ./repo
# Why a.txt looks like this
- [commit] T1 c20826e9 2026-11-10 10:19 — planloop: T1 写 a

$ planloop why b.txt:1 -w ./repo
- [commit] T2 62111962 2026-11-10 10:19 — planloop: T2 写 b
```

两个数据源，各有不可替代性：

| 源 | 能回答 | 不能回答 |
|---|---|---|
| git blame / log | 当前文件内容是谁提交的（含行级） | 被回滚掉的尝试（它们没有 commit） |
| events.jsonl | 已回滚的尝试、每次写文件动作 | 精确到行的归属 |

所以 `blame()` 先取 git，再把"从未进入 commit 的已回滚尝试"从事件日志补上。
如果没有 git 仓库，就退化为纯事件归属，并在报告里写明这一点。

## 二、审计：回答"仓库为什么长这样"

### 三层证据链

| 层 | 记录什么 | 存放 | 要求 |
|---|---|---|---|
| 决策审计 | 计划版本、路由决策、重规划理由、被否掉的备选 | `events.jsonl` + `plans/vN.json` | append-only |
| 变更审计 | 哪个节点改了哪些文件、diff、依据哪条验收 | git commit + `node_id` | 一次提交 = 一个节点的成果 |
| 验证审计 | 每条验收的**原始输出** | `evidence/<node>/<crit>.log` | 存原文，不存 "PASS" |

### 证据分级（审计报告必须标注）

| 级别 | 内容 | 可复现 |
|---|---|---|
| A | 测试 / 编译 / 命令退出码 | 是 |
| B | schema、文件内容、键存在性 | 是（弱） |
| C | 模型评审（带原文引用） | 否 |
| D | 模型自述"我做完了" | 否，**不采信** |

一次"成功"如果终验只有 C/D 级证据，报告必须写明：**这次成功不可验证**。

### 审计入口（计划）

```bash
planloop audit <run-id>            # 时间线 Markdown：目标 → 计划 → 节点 → 验证 → 回滚
planloop audit <run-id> --blame    # 文件级：某文件被哪些节点、哪次尝试改过
planloop why src/x.py:42           # blame 到节点 + 当时的验收证据
```

输出应该能直接贴进 PR 描述——这是它最实用的形态。

## 三、回退：建立在 git 提交点上（已实现）

`GitWorkspace`（`planloop/workspace.py`）提供的接口：

| 方法 | 作用 |
|---|---|
| `start(run_id, allow_dirty=False)` | 建/切 `planloop/<run_id>` 分支；脏工作区默认拒绝，`allow_dirty` 则先 stash |
| `commit(node_id, label)` | 每节点一次提交，信息带 `planloop-node: <id>` |
| `stash(label)` | 保存失败产物（`--include-untracked`），供审计取回 |
| `reset(ref)` | `reset --hard` + `clean -fd`，回到绿点 |
| `changed_files(ref)` / `log()` / `stashes()` | 审计查询 |

调度器在每个节点失败后自动执行：**stash → 回滚到进入该节点时的 HEAD**，所以重试总是从干净状态开始。
提交 sha 记录在 `state.json` 的 `checkpoints` 里，重启后仍可定位。

实测（`examples/git_demo.py`）：

```
node T1: done  → commit "planloop: T1 make a"
node T2: failed → stash "planloop T2 attempt 1 failed" → reset

a.txt exists: True    (T1 的成果保留)
b.txt exists: False   (失败产物已回滚)
git log:  cf6c253 planloop: T1 make a
          0393e48 base
stash:    planloop T2 attempt 1 failed
```

### 分支与提交点

```
main
 └── planloop/run-8f2a             独立分支，不污染主历史
      ├── ckpt/T1   ✅
      ├── ckpt/T2   ✅
      ├── ckpt/M1   ✅ 里程碑（全量测试通过）
      ├── ckpt/T3   ❌ 失败 → 自动回滚到 ckpt/T2
      └── ckpt/T4
```

提交信息携带可追溯字段：

```
planloop: T3 add export_csv

run: r-8f2a      node: T3      attempt: 2
acceptance: A1 exit=0 (sha1:41099aa)
plan: v2 (第 3 次修订，原因: interface_mismatch)
reverted: T2 (reason: execution_failure)
```

### 三种粒度

| 粒度 | 动作 | 触发条件 |
|---|---|---|
| 单节点 | `reset --hard ckpt/T2`，只重跑 T3 | 执行错 |
| 子树 | 回到子树起点，重分解该子树 | 计划错、接口不匹配 |
| 全局 | 回到 `ckpt/M0`，换规格或换计划 | 规格错、假设错、连续失败 |

### 三条铁律

1. **回滚前先 stash**：失败产物是审计证据，`reset --hard` 会连证据一起删掉。
2. **回滚不删历史**：失败写进 negative memory（"走 A 方案会撞上 X"），否则重犯。
3. **全局回滚必须人工批准**：局部可自动，全局等于否定前面全部工作。

## 四、文档回退的两种含义

| | 是什么 | 怎么管 |
|---|---|---|
| 仓库里的文档（README/ADR/注释） | 代码的一部分 | 与代码同一套提交点，天然可回退 |
| 计划与记忆元数据 | 运行时产物 | 单独版本化，见下 |

- **计划版本化（P9 待实现）**：每次修订写 `plans/vN.json` 并保留结构化 diff，可回答“第 2 次修订改了什么”。
- **记忆只可失效不可删除**：靠 `status` 与 `trust` 退化，删除等于销毁"曾经犯过"的证据。
- **审计日志只追加**：更正是新记录，不是改旧行。

## 五、错误处理状态机

```
检测失败
  → 分类（规格 / 假设 / 计划 / 执行）       复用的 FailureClass
  → 算影响半径（依赖闭包）                   从 DAG 得出需回退的节点集
  → 选回退粒度（节点 / 子树 / 全局）
  → stash 失败产物 → 回滚 → 记录回退原因
  → 重规划（有上限）→ 写 negative memory
  → 同一里程碑重规划 ≥2 次仍失败 → 停机问人
```

最后一条是止损线：**系统可以自己修小错，但不能自己决定大方向。**

## 六、剩余实现清单

| 项 | 预计工作量 | 依赖 |
|---|---|---|
| 动态计划修订与结构化 diff | 待评估 | 事件日志 + 持久化 |
| 计划修订影响集与选择性重跑 | 待评估 | DAG + 节点状态 |
| 计划版本审计视图 | 待评估 | `audit.py` |
| 全局回滚的人工批准 | 待评估 | 安全闸门 |

`workspace.py` 检查点、stash/rollback、`audit.py`、`audit`/`why` CLI 和证据分级已经实现，不再列为待办。

## 七、动态计划修订（P9）

### 7.1 版本而不是原地修改

任何结构变化都产生新的不可变计划版本：

```json
{
  "version": 2,
  "parent_version": 1,
  "reason": "T2 的 API 版本假设与探测结果不符",
  "operations": [
    {"op": "split", "node": "T2", "into": ["ProbeAPI", "T2a", "T2b"]},
    {"op": "add_dependency", "node": "T3", "depends_on": "T2b"}
  ],
  "affected": ["T2", "ProbeAPI", "T2a", "T2b", "T3"],
  "budget_delta": {"estimated_cost": 0.12},
  "parent_fingerprint": "...",
  "fingerprint": "..."
}
```

落盘布局：

```text
<run>/plans/v1.json
<run>/plans/v2.json
<run>/plans/v1-v2.diff.json
```

当前版本只通过状态中的 `plan_version` 指针选择。修订验证失败时不移动指针，因此不会留下半应用计划。

### 7.2 v1 操作集合

| 操作 | 语义 | 关键限制 |
|---|---|---|
| `add` | 增加节点 | ID 唯一，依赖必须存在 |
| `replace` | 用新定义替换未完成节点 | 已完成节点只能通过显式失效处理 |
| `split` | 一个节点拆成有序子图 | 必须重新连接原入边和出边 |
| `cancel` | 取消不再需要的节点 | 不得留下活动节点依赖已取消节点 |
| `add_dependency` | 增加依赖边 | 不得形成环或超过结构上限 |

自动合并节点和动态重组里程碑不进入 v1。

### 7.3 影响范围与状态迁移

修订操作直接涉及的节点构成变更集 `changed`；其所有下游依赖闭包构成 `affected`。状态按以下规则迁移：

| 节点 | 修订后状态 |
|---|---|
| 已完成且不在 `affected` | 保持 `done`，复用产物与证据 |
| 位于 `affected` | 重置为 `pending`，旧证据保留在历史中 |
| 新增节点 | `pending` |
| 取消节点 | `cancelled`，不参与就绪队列 |
| 修订时正在运行 | 先停止并记录不确定结果，再按 `affected` 处理 |

选择性重跑只复用具有完整版本、产物和验收证据的节点；证据缺失时采取保守策略重新执行。

### 7.4 应用前校验

候选版本必须重新通过完整静态编译，至少检查：

- DAG 无环、无悬空依赖、自依赖；
- 节点和里程碑引用有效；
- 输入输出契约仍闭合；
- 每个活动节点仍有验收条件；
- 不可逆节点仍有人类闸门；
- 深度、扇出、节点数和估算成本未超限；
- 剩余预算足以覆盖新增成本。

只有全部校验通过后，才能原子切换 `plan_version` 并重置影响集。

### 7.5 防震荡

除了既有的子树和全局重规划上限，再增加三道约束：

1. 对规范化计划计算指纹；运行中出现过的指纹不得再次成为当前版本；
2. 记录规范化操作签名，连续重复同一修订意图时停止并问人；
3. 同一失败签名经过两次结构修订仍未消失时，不再自动修订。

因此 A→B→A、反复拆分同一节点或只改名字不改依赖的“伪修订”都会触发止损。

### 7.6 验收场景

基准计划为 `T1 → T2 → T3`。T2 发现接口假设错误后，修订为：

```text
T1 → ProbeAPI → T2a → T2b → T3
```

验收要求：T1 不重跑；新节点和 T3 按新依赖执行；两个计划快照及 diff 可审计；预算增量可见；循环、超预算和重复指纹修订均被拒绝且不改变当前版本。
