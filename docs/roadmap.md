# 路线图

每个阶段只有一个验收门，**不过门不进入下一阶段**。

> 章节按**实际完成顺序**排列；编号是引入时的序号，所以会出现 P5 在 P4 前面（安全闸门比路由先做）。编号不重排，避免把其它文档里的引用改乱。

## 当前：v0 骨架（已完成）

```
spec → decompile → compile → execute → verify → replan
     + 三层验收 + 预算熔断 + 错误记忆（软提示 / 硬护栏）
```

验收门（已通过）：

- 53 项单测全绿；
- `planloop demo` 四组对照可复现，且对照组（无记忆）确实把回归留在了仓库里；
- 拒绝路径可验证：无验收标准、不可逆无闸门、预算超支都会停在编译期。

## M1 持久记忆（已完成）

SQLite + FTS5 + 向量，公有/私有分库，证据驱动，仓库作用域隔离。

- [x] `memdb.py`：schema、混合检索、TTL、证据复验
- [x] `embed.py`：特征哈希 embedder（零依赖）+ 远端 embedding 适配
- [x] `cli mem add/search/list/stats/verify/promote/forget`
- [x] `execute` 注入 top-3 事实；`scheduler` 收尾写 episode
- [x] 写决策 ADD/UPDATE/NOOP/DELETE + `memory_history` 变更轨迹
- [x] 双时态 `valid_from` / `valid_to`
- [x] 可执行 procedure（`exec_spec` + `mem run`）
- [x] `mem export` 导出 AGENTS.md
- [ ] M2 事实自动抽取 + 定时复验
- [ ] M3 决策记录
- [ ] M4 程序记忆归纳
- [ ] M5 检索预算调优

---

## P0 持久化与续跑（已完成）
**为什么排第一**：进程退出即丢状态，长任务根本跑不完一次。

- [x] `persist.py`：`state.json` 原子写入、`RunState` 快照与恢复
- [x] 每次状态迁移落盘（节点开始/完成/失败/重规划/预算耗尽）
- [x] `run --resume [run-id]`：跳过已完成节点，恢复产物、预算、重规划计数
- [x] `runs list`：列出所有运行与进度
- [x] 崩溃中的 `running` 节点视为未完成，重做而非相信半成品

**验收**：`examples/resume_demo.py` 与 `tests/test_persist.py`——首轮 T1 成功、T2 失败并阻塞；续跑只执行 T2 并完成，T1 不重复执行。

---

## P1 git 提交点与回滚（已完成）

**为什么排第二**：没有回退点，长任务的风险不可控。

- [x] `workspace.py`：clean 检查、`commit(node)`、`stash`、`reset`、`changed_files`
- [x] 独立分支 `planloop/<run_id>`，不污染主历史
- [x] 每节点一次 commit，信息带 `planloop-node: <id>`
- [x] 失败先 stash（保留证据）再 `reset --hard` 到上个绿点
- [x] 提交 sha 记入 `state.json` 的 `checkpoints`

**验收**：`examples/git_demo.py` 与 `tests/test_git_workspace.py`——T1 提交后 T2 失败，工作区回到 T1 状态，b.txt 被 stash 而非删除。

- [x] 审计报告：`audit.py` + `planloop audit`，每次运行自动生成 `AUDIT.md`（进度/缺口/时间线/证据等级）
- [x] `why` / blame：`blame.py` + `planloop why <path>[:line]`，git 行级归属 + 已回滚尝试的事件补全
- [ ] 计划版本化（replan 前后的 diff）仍未做，见 [审计与回滚](audit-rollback.md)

---

## P2 上下文压缩与事实回写（已完成）

**为什么排第三**：第 30 步之后还准不准，取决于摘要质量。

- [x] `compress.py`：模型摘要（`task=compress`）+ 抽取式兜底，超预算时按段压缩
- [x] `Blackboard.context_for` 接入压缩器，硬预算 1400 字符不变
- [x] 事实自动回写：通过的测试命令、节点写出的文件（证据先验证再入库，每 run 上限 3 条）
- [x] `Compressor.stats`：calls / tokens / chars_in / chars_out / ratio / fallbacks

**验收**：`tests/test_compress.py`——超预算上下文 ≤ 预算且保留 `ModuleNotFoundError`；`test_command` 在命令确实通过时入库、失败时不入库。

- [ ] 未做：用真实长任务验证"第 30 步上下文 ≤ 50%"（需要先把 P3 的三层计划做出来）

---

## P3 里程碑层（已完成）

**为什么排第四**：防方向性错误，需要先有 P0/P1 兜底。

- [x] `decomp.Milestone` + `TaskNode.milestone`，计划序列化含里程碑状态
- [x] 里程碑级验收（通常跑全量测试），在节点全部完成后触发
- [x] 人工闸门：默认拒绝，`--auto-approve` 或自定义 `milestone_gate` 放行
- [x] 通过后单独打一个 commit（`planloop: M1 ...`）
- [x] 编译期校验：引用未定义里程碑 → `E_UNKNOWN_MILESTONE`

**验收**：`examples/milestone_demo.py` 与 `tests/test_milestones.py`——两个节点各自的验收都通过，里程碑的全量测试失败，运行停在里程碑并给出真实测试输出；未获批准时停在 `M1:blocked`。

---

## P5 安全闸门（已完成）

**为什么在路由之前**：benchmark 会真的动文件，而原来的 `Tools.run` 是裸的。

- [x] `policy.py`：三级裁决（allow / confirm / deny），规则可解释，未知 shell 结构 fail-closed
- [x] `approvals.py`：一次性、绑定命令哈希 + run + 过期的批准，append-only 事件流
- [x] `tools.py`：执行/写入前拦截，这是唯一出口
- [x] `scheduler.py`：拦下后写 pending 批准、停跑、给出下一步命令；`--resume` 后继续
- [x] `planloop approvals list|approve|deny`
- [x] 审计时间线记录"等待批准 / 策略拒绝"
- [ ] 交互式 TTY 确认、`policy.toml` 仓库级自定义规则

**验收**：`tests/test_policy.py`——批准前 `scratch/` 原封不动，批准后续跑才执行；`deny` 级动作不产生批准路径；续跑不丢历史事件。

详见 [安全闸门](safety.md)。

---

## P6 执行沙箱（基础版已完成）

**为什么现在做**：策略只能拦"看起来危险的命令"，拦不住"读环境变量再外发"。

- [x] `sandbox.py`：`HostSandbox`（环境变量脱敏）、`NoExecSandbox`、`DockerSandbox`（无网络、只挂工作区）
- [x] `--sandbox host|none|docker`；Docker 不可用时直接报错，不静默退回
- [x] 工具层统一走 sandbox，验收命令同样受限
- [ ] 强隔离：E2B 兼容后端（可指向 CubeSandbox）、快照回滚、网络白名单 + 凭据代理

**验收**：`tests/test_sandbox.py`——子进程读不到 `*_TOKEN`/`*_KEY`；`none` 下命令一律 `exit=126`；Docker 缺失时显式失败。

详见 [沙箱](sandbox.md)。强隔离不在 Windows 本机实现——没有 Linux + KVM，写了也验证不了。

---

## P4 模型分档路由（已完成）

**为什么排最后**：长任务里省钱不是主要矛盾；且需要 P0 的 trace 才能校准 `p_w`。

- [x] `routing.py`：cheap / standard / strong / verify，档位缺失时按 standard → cheap → strong 回落
- [x] 失败升级（attempt>0 → strong）+ 记忆命中抬档 + 决策写事件日志（`route`）
- [x] 加权成本 `cost_units = Σ tokens × weight`
- [x] 验证器隔离：`verify` 档与执行档实例重合时返回 `None`
- [x] 三组对照实验（`examples/routing_demo.py`），两种工作量

**验收结果**：

| 工作量 | 全 cheap | 全 strong | 路由 |
|---|---|---|---|
| 1 易 + 1 难 | 失败，cost 174 | 成功，2313 | 成功，**2151**（省 7%） |
| 4 易 + 1 难 | 失败，413 | 成功，5895 | 成功，**2390**（省 59%） |

未达"≤ all-strong × 0.6"的场景（1 易 + 1 难，省 7%）是真实结论，不是缺陷：**路由的收益与简单任务占比成正比**。成本为权重假设，接真实模型需按真实单价重算。

详见 [模型分档路由](routing.md)。

---

## P7 对照基准（已完成）

**要回答**：同样的模型、同样的预算下，"验证 + 重试"是否比"多发几次投票"划算。

- [x] `bench.py`：三个真实任务（修 bug / 加功能 / 重命名）、B0/B1/B2 三种配置
- [x] 独立 oracle（跑测试）判定成功，不由被测配置自评
- [x] 固定随机源，结果可复现
- [x] `planloop bench --reps/--seed/--p-correct/--out`

**结果**（脚本模型 p=0.6，reps=10，seed=7）：

| config | 成功率 | 平均 tokens |
|---|---|---|
| B0 | 63% | 134 |
| B1 | 73% | 668 |
| B2 | 80% | 1246 |

**未完成**：接真实模型（需 `PLANLOOP_MODEL` 等凭据）。没有真实模型前，这些数字只证明 harness 正确，不证明模型效果——报告里已写明。

详见 [对照基准](bench.md)。

---

## P8 记忆深化

- [ ] 记忆作用域：按仓库隔离 + 全局流程层
- [ ] TTL 与容量上限
- [ ] 冲突检测（同一触发条件下两条相反的 fix）
- [ ] `planloop memory audit`：人可审阅、可撤销

**验收门**：注入一条错误 lesson，系统能通过误杀检测把它降到 `refuted`。

---

## 明确不排期

| 项 | 原因 |
|---|---|
| 多模型辩论 | 收益不抵开销 |
| 模型自评难度路由 | 不可靠 |
| 端到端无人值守 | 方向性错误无人纠正 |
| 自研 token 计费 | 用 API 返回值即可 |
