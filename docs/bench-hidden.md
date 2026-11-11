# Benchmark: B0 / B1 / B2

- repetitions per cell: **5**
- per-attempt success probability of the scripted model: **0.6**
- seed: 0
- sampling temperature: **0.2**

> 没有真实模型凭据时，这些数字验证的是 **harness 与度量管线**，不是模型能力。
> 成功由独立 oracle（跑测试）判定，不由被测配置自评。

| config | success_rate | mean_tokens | runs |
|---|---|---|---|
| B0 | 0% | 14445 | 10 |
| B1 | 0% | 39576 | 9 |
| B2 | 100% | 8322 | 9 |

## 按任务

| task | config | success | tokens |
|---|---|---|---|
| divide_zero_hidden | B0 | no | 4087 |
| divide_zero_hidden | B1 | no | 41434 |
| divide_zero_hidden | B2 | yes | 7704 |
| mean_empty_hidden | B0 | no | 16624 |
| mean_empty_hidden | B1 | no | 41300 |
| mean_empty_hidden | B2 | yes | 8176 |
| divide_zero_hidden | B0 | no | 11303 |
| divide_zero_hidden | B1 | no | 27616 |
| divide_zero_hidden | B2 | yes | 9106 |
| mean_empty_hidden | B0 | no | 13203 |
| mean_empty_hidden | B1 | no | 60075 |
| mean_empty_hidden | B2 | yes | 8356 |
| divide_zero_hidden | B0 | no | 19479 |
| divide_zero_hidden | B1 | no | 38835 |
| divide_zero_hidden | B2 | yes | 7845 |
| mean_empty_hidden | B0 | no | 17030 |
| mean_empty_hidden | B1 | no | 37223 |
| mean_empty_hidden | B2 | yes | 7935 |
| divide_zero_hidden | B0 | no | 13338 |
| divide_zero_hidden | B1 | no | 40611 |
| divide_zero_hidden | B2 | yes | 9123 |
| mean_empty_hidden | B0 | no | 17102 |
| mean_empty_hidden | B1 | no | 34329 |
| mean_empty_hidden | B2 | yes | 7974 |
| divide_zero_hidden | B0 | no | 11521 |
| divide_zero_hidden | B1 | no | 34757 |
| divide_zero_hidden | B2 | yes | 8678 |
| mean_empty_hidden | B0 | no | 20765 |

## 汇总

| config | 成功 | 平均 tokens | 样本 |
|---|---|---|---|
| B0 | 0/10 | 14445 | 10 |
| B1 | 0/9 | 39576 | 9 |
| B2 | 9/9 | 8322 | 9 |

题目：`divide_zero_hidden`（除零必须抛 `ValueError`）、`mean_empty_hidden`（空列表均值必须为 0）。
两条要求都只存在于 held-out 判据里，模型看不见断言，只能靠验收失败输出知道。

对照关系：**B0 与 B1 拿不到失败反馈**（B1 是三次盲猜取多数），B2 能拿到并且会重试。

每个格子的样本数：

| task | config | runs |
|---|---|---|
| divide_zero_hidden | B0 | 5 |
| divide_zero_hidden | B1 | 5 |
| divide_zero_hidden | B2 | 5 |
| mean_empty_hidden | B0 | 5 |
| mean_empty_hidden | B1 | 4 |
| mean_empty_hidden | B2 | 4 |
