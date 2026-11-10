# Benchmark: B0 / B1 / B2

- repetitions per cell: **2**
- per-attempt success probability of the scripted model: **0.6**
- seed: 0

> 没有真实模型凭据时，这些数字验证的是 **harness 与度量管线**，不是模型能力。
> 成功由独立 oracle（跑测试）判定，不由被测配置自评。

| config | success_rate | mean_tokens | runs |
|---|---|---|---|
| B0 | 100% | 3388 | 4 |
| B1 | 100% | 10444 | 4 |
| B2 | 100% | 2627 | 4 |

## 按任务

| task | config | success | tokens |
|---|---|---|---|
| compat_rename | B0 | yes | 1341 |
| compat_rename | B1 | yes | 9841 |
| compat_rename | B2 | yes | 3301 |
| noisy_fix | B0 | yes | 4637 |
| noisy_fix | B1 | yes | 13071 |
| noisy_fix | B2 | yes | 1352 |
| compat_rename | B0 | yes | 3660 |
| compat_rename | B1 | yes | 8296 |
| compat_rename | B2 | yes | 2956 |
| noisy_fix | B0 | yes | 3916 |
| noisy_fix | B1 | yes | 10568 |
| noisy_fix | B2 | yes | 2899 |
