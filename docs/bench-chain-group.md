# Benchmark: B0 / B1 / B2

- repetitions per cell: **1**
- per-attempt success probability of the scripted model: **0.6**
- seed: 0

> 没有真实模型凭据时，这些数字验证的是 **harness 与度量管线**，不是模型能力。
> 成功由独立 oracle（跑测试）判定，不由被测配置自评。

| config | success_rate | mean_tokens | runs |
|---|---|---|---|
| B0 | 17% | 2802 | 6 |
| B2 | 17% | 7129 | 6 |

## 按任务

| task | config | success | tokens |
|---|---|---|---|
| rename_chain_3 | B0 | no | 1540 |
| rename_chain_3 | B2 | no | 8178 |
| rename_chain_4 | B0 | yes | 8062 |
| rename_chain_4 | B2 | yes | 12080 |
| rename_chain_5 | B0 | no | 1690 |
| rename_chain_5 | B2 | no | 8014 |
| rename_chain_6 | B0 | no | 1765 |
| rename_chain_6 | B2 | no | 1116 |
| rename_chain_7 | B0 | no | 1840 |
| rename_chain_7 | B2 | no | 5368 |
| rename_chain_8 | B0 | no | 1915 |
| rename_chain_8 | B2 | no | 8017 |
