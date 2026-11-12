# planloop

把"计划"做成**可校验的数据**，让 agent 的每一步都能被独立验证、被记忆约束、被预算叫停。

```bash
python -m planloop demo      # 四组对照实验，约 20 秒
python -m unittest discover -s tests -t .   # 337 项测试
```

核心主张只有一句：**在「模型自己宣布完成、但其实不算完成」的那一类失效上，主因不是规划器不够聪明，而是计划不可验证。** 所以这个项目把力气花在"验收、记忆、回滚"上，而不是提示词。

> 这句话的范围是实测出来的，不是设计出来的：四臂消融显示验收 + **带诊断的失败反馈**把这类任务的成功率从 0/6 拉到 6/6（`p=1.08e-3`），而"约定写在文档里"这类失效模型自己就能读懂（12/12 全过），**测不出差别**。
> 主张层面的判断、既有判断要改哪、优先级怎么重排 → [项目主张分析报告](docs/analysis.md)。

---

## 一、当前能力

| 模块 | 文件 | 状态 | 说明 |
|---|---|---|---|
| 输入规格 | `spec.py` | ✅ 已实现 | 没有验收标准直接拒绝，转为提问 |
| 预算 | `budget.py` | ✅ | 阶段软配额 + 全局硬上限，超支抛异常 |
| 状态板 | `blackboard.py` | ✅ | append-only 事件日志 + 上下文切片 |
| 模型适配 | `model.py` | ✅ | 脚本模型 / OpenAI 兼容 / 按标签路由 |
| 工具层 | `tools.py` | ✅ | 唯一触达文件与子进程的出口，路径锁定 |
| 三层验收 | `verify.py` | ✅ | L1 确定性 / L2 评委（须引用原文）/ L3 人工 |
| 分解 | `decomp.py` | ✅ | HTN → DAG，深度/扇出/规模硬限制 |
| 静态校验 | `compile.py` | ✅ | 环、孤儿、接口、成本、不可逆闸门、**记忆护栏注入** |
| 执行 | `execute.py` | ✅ | 单节点 ReAct 内环，逐步复核 L1 |
| 重规划 | `replan.py` | ✅ | 五类失败分类 + 次数上限 |
| 调度 | `scheduler.py` | ✅ | 就绪队列 → 执行 → 验证 → 阻断报告 |
| **里程碑层** | `decomp.py`、`scheduler.py` | ✅ | 里程碑级全量验证 + 人工闸门 + 独立提交点 |
| **运行持久化** | `persist.py` | ✅ | `state.json` 原子落盘，`--resume` 跳过已完成节点 |
| **git 提交点** | `workspace.py` | ✅ | 每节点一次 commit，失败先 stash 再回滚到上个绿点 |
| **审计报告** | `audit.py` | ✅ | 从 state + events + git 生成 Markdown：进度、缺口、时间线、证据等级 |
| **溯源** | `blame.py` | ✅ | `why <path>[:line]`：哪个节点、哪个提交改的；含已回滚的尝试 |
| **安全闸门** | `policy.py`、`approvals.py` | ✅ | 高危命令/敏感写入默认拦下，一次性批准后才执行（fail-closed） |
| **执行沙箱** | `sandbox.py` | ⚠️ 基础版 | 环境变量脱敏 / 禁止执行 / Docker 无网络容器；**不是隔离边界** |
| **上下文压缩** | `compress.py` | ✅ | 超预算时模型摘要 + 抽取式兜底；节点结束自动回写可验证事实 |
| **错误记忆** | `memory.py` | ✅ | 软提示 + 硬护栏，护栏须实测通过才入库 |
| **持久记忆** | `memdb.py`、`embed.py` | ✅ | SQLite + FTS5 + 向量，公有/私有分库，证据驱动 |
| CLI | `cli.py` | ✅ | `demo / validate / run / runs / audit / why / approvals / memory / mem / signature` |
| **模型分档路由** | `routing.py` | ✅ | cheap/standard/strong/verify，失败升级，加权成本；4 易 + 1 难时省 59% |
| **对照基准** | `bench.py` | ✅ | B0/B1/B2 同任务同预算，判据在工作区外（模型改不到自己的判卷） |
| 并行调度 | — | ❌ 未做 | 就绪队列是串行的 |

---

## 二、四组对照实验（`planloop demo`）

同一类错误，三种应对。B 组是关键：**没有记忆时，一次"状态成功"的运行把回归留在了仓库里。**

| run | 做了什么 | 护栏 | 拦截 | L1 失败 | 步数 | 结束后全量测试 |
|---|---|---|---|---|---|---|
| A_learn | 撞上 `ModuleNotFoundError`，修复，学到一条 lesson | – | 0 | 1 | 4 | 绿 |
| B_no_memory | 同样的错误再来一次，**没有记忆** | – | 0 | 1 | 4 | **红** |
| C_memory_ignored | 有记忆，但模型没听提示 | ✓ | 1 | 2 | 4 | 绿 |
| D_memory_read | 有记忆，模型读了提示 | ✓ | 0 | 0 | 3 | 绿 |

结论：**软提示省步数，硬护栏防事故**。只做前者会漏，只做后者会慢。

---

## 三、四条设计原则

1. **没有验收标准，不进规划。** 一个无法判定的目标应该变成一句提问，而不是一个任务图。
2. **执行者不能给自己发合格证。** 验证器与执行器的模型实例必须不同，构造时断言。
3. **每个能力都要有硬上限。** 深度 4、扇出 7、单节点重试 2、全局重规划 2、token 总量。超限输出阻断报告，而不是继续烧。
4. **状态外置，上下文切片。** 每个节点只看到"自己的目标 + 上游摘要 + 自己的失败史"，绝不看全量历史。

---

## 四、命令

```bash
python -m planloop demo --dir .demo        # 四组对照实验
python -m planloop validate examples/fix_discount.json
python -m planloop run spec.json -w ./repo # 需要 PLANLOOP_MODEL；自动落盘运行状态
python -m planloop runs list -w ./repo     # 所有运行及进度
python -m planloop run --resume -w ./repo  # 续跑最近一次
python -m planloop run spec.json -w ./repo --git   # 每节点一个 commit，失败自动回滚
python -m planloop run spec.json -w ./repo --git --auto-approve   # 里程碑自动放行
python -m planloop audit -w ./repo         # 生成 AUDIT.md：进度 / 缺口 / 时间线 / 证据等级
python -m planloop why src/x.py:42 -w ./repo   # 这一行是哪个节点改的
python -m planloop bench --reps 10 --seed 7   # B0/B1/B2 对照
python -m planloop bench --configs B0,B2C,B2D,B2 --reps 3 --tasks divide_zero_hidden  # 四臂消融
python -m planloop bench --real --model MiniMax-M3.1-Flash --temperature 1.0   # 真实模型 + 采样噪声
python -m planloop faults --out docs/fault-recall.md   # 判据召回率：注入已知缺陷，看判据抓不抓得住
python -m planloop diagnose --demo .demo --since 2026-11-10T17:05 --out docs/limit-diagnosis.md  # 成功率被什么限制
python -m planloop membench --out docs/memory-value-scripted.md # 记忆值不值（脚本模型）：第二次同类任务快多少
python -m planloop membench --real --model MiniMax-M3.1-Flash --reps 8 --out docs/memory-value.md  # 同上，换真实模型
python -m planloop membench --real --family fee --model MiniMax-M3.1-Flash --reps 8 --out docs/memory-value-fee.md  # 契约不可猜那一族：知识必需时才测得出记忆
python -m planloop membench --analyze --family fee --dir .membench-fee9   # 从事件流回读"模型到底做了什么"
python -m planloop membench --verify --family fee --dir .membench-fee9 --report .demo/membench-fee9.md  # 从原始产物重算并交叉核对报告数字
python -m planloop approvals list -w ./repo   # 查看等待批准的高危动作
python -m planloop run spec.json -w ./repo --sandbox docker  # 无网络一次性容器里执行
python -m planloop memory list             # 查看已学到的教训
python -m planloop mem list                # 查看持久记忆（事实/程序/决策/事件）
python -m planloop mem verify              # 重跑证据，失效条目降级
python -m planloop mem history <id>        # ADD/UPDATE/NOOP/DELETE 轨迹
python -m planloop mem run run_tests       # 执行一条可执行 procedure
python -m planloop mem export --out AGENTS.md --yes   # 只导出 A/B 级证据条目
python -m planloop signature failure.log   # 看一段报错会被归一化成什么
```

真实模型通过环境变量接入：

```bash
PLANLOOP_MODEL=deepseek-chat
PLANLOOP_BASE_URL=https://api.deepseek.com/v1
PLANLOOP_API_KEY=...
```

---

## 五、目录

```
planloop/          运行时（纯标准库，无第三方依赖）
tests/             337 项单测（unittest，pytest 亦可跑）
docs/              设计文档，已实现与仅设计分开标注
examples/          可跑的 spec 示例
```

---

## 六、明确不做

| 不做 | 原因 |
|---|---|
| 让模型自评难度再路由 | 模型对自身难度的判断不可靠，等于自我催眠 |
| 多模型辩论 / 投票 | 通信开销吃掉收益 |
| 无验收的"自动完成" | 无法验证的成功只是措辞 |
| 自动执行不可逆操作 | 默认拒绝，需显式开关或人工批准 |

---

## 七、文档

- [架构与数据契约](docs/architecture.md) — 主链路、事件日志、失败决策表
- [错误记忆](docs/memory.md) — 签名、触发、护栏准入、信任衰减
- [记忆系统](docs/memory-system.md) — 事实/程序/决策/事件，SQLite + 向量，私有/公有分库，写决策与历史
- [市面方案调研](docs/market-scan.md) — 与 Mem0 / Zep / AGENTS.md / Reflexion 的对比与设计修订
- [沙箱](docs/sandbox.md) — 现状、选型（含 CubeSandbox）与本次实现
- [安全闸门](docs/safety.md) — 高危动作截留、一次性批准、fail-closed
- [审计与回滚](docs/audit-rollback.md) — 证据分级、git 提交点、三种回退粒度（设计）
- [对照基准](docs/bench.md) — B0/B1/B2 设计与实测（含不能证明什么）
- [项目主张分析报告](docs/analysis.md) — **验证层是不是壁垒**：判断、对既有判断的修正、重排后的优先级
- [实验汇总](docs/bench-summary.md) — 全部实验结果：结论、机制证据、效度威胁、下一步
- [判据召回率](docs/fault-recall.md) — 故障注入集：仓外强判据 33/33，模型自己写的弱判据只有 13/33
- [成功率被什么限制](docs/limit-diagnosis.md) — 归因诊断：193 个真实格子，限制项是需求可见性与诊断载荷，不是重试上限
- [记忆值不值](docs/memory-value.md) — 真实模型（`contract` 族）：**"没测到差异"有一半是装置的毛病**——契约猜得出来
- [记忆值不值（`fee` 族）](docs/memory-value-fee.md) — 契约**不可猜**那一族：知识必需时，记忆到底能省什么
- [记忆值不值（脚本模型）](docs/memory-value-scripted.md) — 脚本模型版：教训+护栏与已验证事实两条路径**收益重合**
- [held-out 判据实测](docs/bench-hidden.md) — 模型自己宣布完成时，验收 + 重试把 0% 拉到 100%
- [消融：验证 vs 执行器](docs/bench-ablation.md) — 关掉强制验收，同一个执行器退回 0/6
- [四臂消融：反馈通道](docs/bench-ablation2.md) — 告诉它失败不够，失败输出里得带诊断（B2D 0/5 vs B2 6/6）
- [文档约定类陷阱（未触发）](docs/bench-trap.md) — 约定写在 README 里拦不住会读文档的模型
- [模型分档路由](docs/routing.md) — 可判定信号、升级税、实测成本对比
- [执行沙箱](docs/sandbox.md) — 现状、选型（含 CubeSandbox）与本次实现
- [已知失效模式](docs/limits.md) — 这套东西什么时候会害你
- [路线图](docs/roadmap.md) — 阶段划分与验收门
