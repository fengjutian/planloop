# Benchmark: B0 / B1 / B2

> **这份是三臂版本（B0 / B2C / B2）。四臂版本（多了 B2D）在
> [bench-ablation2.md](bench-ablation2.md)** —— 那一版才能把"失败反馈"里的
> "有重试机会"和"失败输出带诊断"拆开，本篇只拆了"有/无反馈"。

- repetitions per cell: **3**
- per-attempt success probability of the scripted model: **0.6**
- seed: 0
- sampling temperature: **0.2**

> 没有真实模型凭据时，这些数字验证的是 **harness 与度量管线**，不是模型能力。
> 成功由独立 oracle（跑测试）判定，不由被测配置自评。

| config | success_rate | mean_tokens | runs |
|---|---|---|---|
| B0 | 0% | 14585 | 6 |
| B2 | 100% | 8360 | 6 |
| B2C | 0% | 7234 | 6 |

## 按任务

| task | config | success | tokens |
|---|---|---|---|
| divide_zero_hidden | B0 | no | 8215 |
| divide_zero_hidden | B2 | yes | 8568 |
| divide_zero_hidden | B2C | no | 6426 |
| mean_empty_hidden | B0 | no | 19585 |
| mean_empty_hidden | B2 | yes | 9717 |
| mean_empty_hidden | B2C | no | 7608 |
| divide_zero_hidden | B0 | no | 15183 |
| divide_zero_hidden | B2 | yes | 7226 |
| divide_zero_hidden | B2C | no | 7201 |
| mean_empty_hidden | B0 | no | 14130 |
| mean_empty_hidden | B2 | yes | 7873 |
| mean_empty_hidden | B2C | no | 7397 |
| divide_zero_hidden | B0 | no | 10364 |
| divide_zero_hidden | B2 | yes | 8871 |
| divide_zero_hidden | B2C | no | 6514 |
| mean_empty_hidden | B0 | no | 20032 |
| mean_empty_hidden | B2 | yes | 7905 |
| mean_empty_hidden | B2C | no | 8259 |

## 汇总

| config | 成功 | 平均 tokens | 样本 |
|---|---|---|---|
| B0 | 0/6 | 14585 | 6 |
| B2C | 0/6 | 7234 | 6 |
| B2 | 6/6 | 8360 | 6 |

- **B0**：单发 JSON 动作，拿不到命令输出，也没有验收。
- **B2C**：planloop 执行器（每步重新注入上下文、同一套工具、提前停止），但节点验收换成永远通过的桩 —— 模型看不到失败，也拿不到重试。
- **B2**：同上，但节点验收是 held-out 判据，失败输出会喂回模型并触发一次重试。

三个配置的最终成功判定都是同一个 held-out 判据，**唯一变化的是模型有没有被告知失败**。

每个格子的样本数：

| task | config | runs |
|---|---|---|
| divide_zero_hidden | B0 | 3 |
| divide_zero_hidden | B2 | 3 |
| divide_zero_hidden | B2C | 3 |
| mean_empty_hidden | B0 | 3 |
| mean_empty_hidden | B2 | 3 |
| mean_empty_hidden | B2C | 3 |
