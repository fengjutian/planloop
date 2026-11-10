# Benchmark: B0 / B1 / B2

- repetitions per cell: **10**
- per-attempt success probability of the scripted model: **0.6**
- seed: 7

> 没有真实模型凭据时，这些数字验证的是 **harness 与度量管线**，不是模型能力。
> 成功由独立 oracle（跑测试）判定，不由被测配置自评。

| config | success_rate | mean_tokens | runs |
|---|---|---|---|
| B0 | 63% | 134 | 30 |
| B1 | 73% | 668 | 30 |
| B2 | 80% | 1246 | 30 |

## 按任务

| task | config | success | tokens |
|---|---|---|---|
| fix_discount | B0 | yes | 84 |
| fix_discount | B1 | yes | 416 |
| fix_discount | B2 | yes | 454 |
| add_feature | B0 | yes | 170 |
| add_feature | B1 | yes | 850 |
| add_feature | B2 | no | 2899 |
| rename_symbol | B0 | yes | 149 |
| rename_symbol | B1 | yes | 741 |
| rename_symbol | B2 | yes | 1064 |
| fix_discount | B0 | yes | 84 |
| fix_discount | B1 | yes | 412 |
| fix_discount | B2 | yes | 454 |
| add_feature | B0 | yes | 170 |
| add_feature | B1 | yes | 850 |
| add_feature | B2 | yes | 765 |
| rename_symbol | B0 | yes | 149 |
| rename_symbol | B1 | yes | 741 |
| rename_symbol | B2 | yes | 1064 |
| fix_discount | B0 | yes | 84 |
| fix_discount | B1 | no | 408 |
| fix_discount | B2 | yes | 454 |
| add_feature | B0 | no | 170 |
| add_feature | B1 | yes | 850 |
| add_feature | B2 | yes | 765 |
| rename_symbol | B0 | no | 147 |
| rename_symbol | B1 | yes | 743 |
| rename_symbol | B2 | no | 3173 |
| fix_discount | B0 | no | 80 |
| fix_discount | B1 | no | 408 |
| fix_discount | B2 | yes | 2327 |
| add_feature | B0 | yes | 170 |
| add_feature | B1 | yes | 850 |
| add_feature | B2 | yes | 765 |
| rename_symbol | B0 | yes | 149 |
| rename_symbol | B1 | no | 739 |
| rename_symbol | B2 | yes | 1064 |
| fix_discount | B0 | yes | 84 |
| fix_discount | B1 | yes | 412 |
| fix_discount | B2 | yes | 454 |
| add_feature | B0 | yes | 170 |
| add_feature | B1 | yes | 850 |
| add_feature | B2 | yes | 765 |
| rename_symbol | B0 | no | 147 |
| rename_symbol | B1 | no | 739 |
| rename_symbol | B2 | no | 3173 |
| fix_discount | B0 | yes | 84 |
| fix_discount | B1 | yes | 416 |
| fix_discount | B2 | yes | 454 |
| add_feature | B0 | yes | 170 |
| add_feature | B1 | yes | 850 |
| add_feature | B2 | yes | 765 |
| rename_symbol | B0 | no | 147 |
| rename_symbol | B1 | yes | 743 |
| rename_symbol | B2 | yes | 1064 |
| fix_discount | B0 | no | 80 |
| fix_discount | B1 | yes | 416 |
| fix_discount | B2 | yes | 454 |
| add_feature | B0 | yes | 170 |
| add_feature | B1 | yes | 850 |
| add_feature | B2 | yes | 765 |
| rename_symbol | B0 | yes | 149 |
| rename_symbol | B1 | no | 739 |
| rename_symbol | B2 | yes | 1064 |
| fix_discount | B0 | yes | 84 |
| fix_discount | B1 | yes | 412 |
| fix_discount | B2 | yes | 454 |
| add_feature | B0 | no | 170 |
| add_feature | B1 | no | 850 |
| add_feature | B2 | yes | 765 |
| rename_symbol | B0 | yes | 149 |
| rename_symbol | B1 | yes | 745 |
| rename_symbol | B2 | no | 3173 |
| fix_discount | B0 | no | 80 |
| fix_discount | B1 | yes | 420 |
| fix_discount | B2 | no | 2569 |
| add_feature | B0 | no | 170 |
| add_feature | B1 | no | 850 |
| add_feature | B2 | yes | 765 |
| rename_symbol | B0 | yes | 149 |
| rename_symbol | B1 | no | 739 |
| rename_symbol | B2 | yes | 1064 |
| fix_discount | B0 | no | 80 |
| fix_discount | B1 | yes | 412 |
| fix_discount | B2 | no | 2555 |
| add_feature | B0 | yes | 170 |
| add_feature | B1 | yes | 850 |
| add_feature | B2 | yes | 765 |
| rename_symbol | B0 | no | 147 |
| rename_symbol | B1 | yes | 741 |
| rename_symbol | B2 | yes | 1064 |
