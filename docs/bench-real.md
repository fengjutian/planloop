# Benchmark: B0 / B1 / B2

- repetitions per cell: **2**
- per-attempt success probability of the scripted model: **0.6**
- seed: 0

> 没有真实模型凭据时，这些数字验证的是 **harness 与度量管线**，不是模型能力。
> 成功由独立 oracle（跑测试）判定，不由被测配置自评。

| config | success_rate | mean_tokens | runs |
|---|---|---|---|
| B0 | 100% | 4296 | 6 |
| B1 | 33% | 7489 | 6 |
| B2 | 100% | 2577 | 6 |

## 按任务

| task | config | success | tokens |
|---|---|---|---|
| fix_discount | B0 | yes | 4256 |
| fix_discount | B1 | no | 8596 |
| fix_discount | B2 | yes | 1243 |
| add_feature | B0 | yes | 4567 |
| add_feature | B1 | no | 7900 |
| add_feature | B2 | yes | 2707 |
| rename_symbol | B0 | yes | 5055 |
| rename_symbol | B1 | yes | 5512 |
| rename_symbol | B2 | yes | 3118 |
| fix_discount | B0 | yes | 4306 |
| fix_discount | B1 | no | 7651 |
| fix_discount | B2 | yes | 2642 |
| add_feature | B0 | yes | 3892 |
| add_feature | B1 | no | 7293 |
| add_feature | B2 | yes | 2596 |
| rename_symbol | B0 | yes | 3701 |
| rename_symbol | B1 | yes | 7981 |
| rename_symbol | B2 | yes | 3156 |
