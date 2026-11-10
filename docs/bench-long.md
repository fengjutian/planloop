# Benchmark: B0 / B1 / B2

- repetitions per cell: **1**
- per-attempt success probability of the scripted model: **0.6**
- seed: 0

> 没有真实模型凭据时，这些数字验证的是 **harness 与度量管线**，不是模型能力。
> 成功由独立 oracle（跑测试）判定，不由被测配置自评。

| config | success_rate | mean_tokens | runs |
|---|---|---|---|
| B0 | 0% | 3074 | 1 |
| B1 | 0% | 3074 | 1 |
| B2 | 100% | 14357 | 1 |

## 按任务

| task | config | success | tokens |
|---|---|---|---|
| long_rename_chain | B0 | no | 3074 |
| long_rename_chain | B1 | no | 3074 |
| long_rename_chain | B2 | yes | 14357 |
