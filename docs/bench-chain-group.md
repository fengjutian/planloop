# Benchmark: B0 / B1 / B2

- repetitions per cell: **1**
- per-attempt success probability of the scripted model: **0.6**
- seed: 0

> 没有真实模型凭据时，这些数字验证的是 **harness 与度量管线**，不是模型能力。
> 成功由独立 oracle（跑测试）判定，不由被测配置自评。

| config | success_rate | mean_tokens | runs |
|---|---|---|---|
| B0 | 100% | 12852 | 6 |
| B2 | 33% | 6612 | 6 |

## 按任务

| task | config | success | tokens |
|---|---|---|---|
| rename_chain_3 | B0 | yes | 3714 |
| rename_chain_3 | B2 | yes | 6475 |
| rename_chain_4 | B0 | yes | 4177 |
| rename_chain_4 | B2 | no | 5575 |
| rename_chain_5 | B0 | yes | 17177 |
| rename_chain_5 | B2 | no | 8904 |
| rename_chain_6 | B0 | yes | 16848 |
| rename_chain_6 | B2 | yes | 7425 |
| rename_chain_7 | B0 | yes | 11640 |
| rename_chain_7 | B2 | no | 2094 |
| rename_chain_8 | B0 | yes | 23557 |
| rename_chain_8 | B2 | no | 9196 |
