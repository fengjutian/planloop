# Benchmark: B0 / B1 / B2

- repetitions per cell: **2**
- per-attempt success probability of the scripted model: **0.6**
- seed: 0

> 没有真实模型凭据时，这些数字验证的是 **harness 与度量管线**，不是模型能力。
> 成功由独立 oracle（跑测试）判定，不由被测配置自评。

| config | success_rate | mean_tokens | runs |
|---|---|---|---|
| B1 | 100% | 11168 | 6 |

## 按任务

| task | config | success | tokens |
|---|---|---|---|
| fix_discount | B1 | yes | 12819 |
| add_feature | B1 | yes | 12581 |
| rename_symbol | B1 | yes | 9938 |
| fix_discount | B1 | yes | 9370 |
| add_feature | B1 | yes | 13309 |
| rename_symbol | B1 | yes | 8992 |
