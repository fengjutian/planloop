# 模型分档路由（设计，部分实现）

> 状态：`model.RoutingModel` 已实现（按 prompt 标签选模型，单层）。
> **分档路由（cheap/standard/strong + 失败升级 + 加权成本）尚未实现。**

## 一、先拆命题

"不同的问题交给不同的模型"其实是三件事：

| 机制 | 解决什么 | 收益 | 代价 |
|---|---|---|---|
| 静态路由 | 成本错配（用强模型做摘要） | 省钱 | 错一次就多一次失败 |
| 反应式升级 | 能力不足 | 提成功率 | 升级税 + 上下文污染 |
| 多模型协作 | 单次采样方差 | 降方差 | 协调开销，多数为负 |

**结论：短任务里路由的第一收益是成本；长任务里优先级下降**——长任务一次返工几十步，省下的单价不抵一次方向错误。

## 二、哪些信号可判定

| 信号 | 来源 | 可靠性 | 现状 |
|---|---|---|---|
| `task=plan/act/judge/lesson` | prompt 标签 | 确定 | ✅ `model.task_tag` |
| 失败分类 | verifier reason code | 确定 | ✅ `replan.classify` |
| 尝试次数 | 调度器 | 确定 | ✅ `node.attempts` |
| `est_cost` / `risk` | 规划期估计 | 中（是规划器的猜测） | ✅ `TaskNode` |
| 同类任务历史成败 | 记忆库 | 高（有证据） | ✅ `memory.match` |
| **问题难度自评** | 模型 | **不可靠** | ❌ 故意不做 |

最后一行是关键：把难度判断交给最没有资格判断的一方，是这类方案最常见的自欺。

## 三、决策表

| 条件 | 档位 | reason |
|---|---|---|
| `task=plan` / 重新分解 | strong | `plan_role` |
| `task=lesson` / 摘要 | cheap | `lesson_role` |
| `task=judge` | verify | `verify_role` |
| `act`，attempt=0，`est_cost<0.35` 且 `risk=low` | cheap | `trivial_node` |
| `act`，attempt=0，其余 | standard | `default` |
| `act`，attempt≥1 | strong | `escalate_after_failure` |
| 命中 lesson（同类翻车史） | 至少 standard；`strength=guard` 则 strong | `memory_lesson` |

**硬约束**：`verify` 档不得与任何执行档共享模型实例，构造时断言。

## 四、升级税

设弱模型单次成本 `c_w`、成功率 `p_w`，强模型 `c_s`，两级策略期望成本 ≈ `c_w + (1−p_w)·c_s`：

| p_w | c_w/c_s | 两级期望 | 直接强模 | 结论 |
|---|---|---|---|---|
| 0.9 | 0.2 | 0.30 | 1.0 | 省 70% |
| 0.7 | 0.2 | 0.50 | 1.0 | 省 50% |
| 0.5 | 0.5 | 0.75 | 1.0 | 勉强省 |
| 0.4 | 0.7 | 1.12 | 1.0 | **更贵** |

**路由唯一真正要回答的问题就是 `p_w`。** 它只能从 trace 里量，不能猜。

被系统性低估的两项成本：
1. 弱模型失败留下的是"做错的事"（错误代码、被改坏的测试），强模型接手要先清理；
2. 升级会让上下文变长（失败历史被带入），进一步抬高单次成本。

## 五、成本口径

```
cost_units = Σ_tier (tokens_tier × weight_tier)
```

不同模型单价差几十倍，只数 token 等于没算成本。权重必须可配置，并在报告里写清是**真实单价**还是**假设值**。

## 六、实验设计

同一任务集，四组：

| 组 | 配置 | 证明什么 |
|---|---|---|
| A | all-cheap | 成功率下限 |
| B | all-strong | 成本上限 |
| C | routed | 是否落在帕累托前沿 |
| D | routed + memory | 是否比 C 少失败 |

通过标准：C 的成功率 ≥ A、加权成本 ≤ B×0.6、且点不落在 A–B 连线之上。

## 七、实现清单

| 项 | 工作量 |
|---|---|
| `routing.py`：`QuestionKind`、`RouteDecision`、`ModelRouter.decide/complete/usage` | 2h |
| 接线 `execute.py` / `scheduler.py`，决策写进事件日志 | 1h |
| 验证器隔离断言（复用 `Verifier.assert_independent`） | 0.2h |
| 四组对照实验 | 1.5h |
| 记忆驱动路由（lesson 命中抬高起始档） | 1h |

**必须先有决策日志**：`{"kind":"route","node":"T1","tier":"cheap","reason":"trivial_node"}`。
没有它，路由既不可归因，也无法做 A/B。
