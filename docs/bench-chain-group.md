# Benchmark: B0 / B1 / B2

- repetitions per cell: **3**
- per-attempt success probability of the scripted model: **0.6**
- seed: 0

> 没有真实模型凭据时，这些数字验证的是 **harness 与度量管线**，不是模型能力。
> 成功由独立 oracle（跑测试）判定，不由被测配置自评。

| config | success_rate | mean_tokens | runs |
|---|---|---|---|
| B0 | 100% | 14837 | 16 |
| B2 | 100% | 21176 | 16 |

## 按任务

| task | config | success | tokens |
|---|---|---|---|
| rename_chain_3 | B0 | yes | 9609 |
| rename_chain_3 | B2 | yes | 17542 |
| rename_chain_4 | B0 | yes | 16027 |
| rename_chain_4 | B2 | yes | 18261 |
| rename_chain_5 | B0 | yes | 20154 |
| rename_chain_5 | B2 | yes | 19594 |
| rename_chain_6 | B0 | yes | 19002 |
| rename_chain_6 | B2 | yes | 21297 |
| rename_chain_7 | B0 | yes | 20010 |
| rename_chain_7 | B2 | yes | 23075 |
| rename_chain_8 | B0 | yes | 12278 |
| rename_chain_8 | B2 | yes | 23510 |
| rename_chain_3 | B0 | yes | 12670 |
| rename_chain_3 | B2 | yes | 17302 |
| rename_chain_4 | B0 | yes | 16694 |
| rename_chain_4 | B2 | yes | 18475 |
| rename_chain_5 | B0 | yes | 22722 |
| rename_chain_5 | B2 | yes | 20426 |
| rename_chain_6 | B0 | yes | 4939 |
| rename_chain_6 | B2 | yes | 20731 |
| rename_chain_7 | B0 | yes | 19739 |
| rename_chain_7 | B2 | yes | 23301 |
| rename_chain_8 | B0 | yes | 19168 |
| rename_chain_8 | B2 | yes | 23881 |
| rename_chain_3 | B0 | yes | 2302 |
| rename_chain_3 | B2 | yes | 18627 |
| rename_chain_4 | B0 | yes | 10684 |
| rename_chain_4 | B2 | yes | 31067 |
| rename_chain_5 | B0 | yes | 12011 |
| rename_chain_5 | B2 | yes | 20102 |
| rename_chain_6 | B0 | yes | 19389 |
| rename_chain_6 | B2 | yes | 21623 |

## 数据来源

由 `.demo/chain3.log` 重新统计生成（MiniMax-M3.1-Flash 真实模型）。

这一轮跑在 `aa2d38d` 之后：链任务的**按节点收窄 context_files 被移除**、
`max_steps_per_node` 从 4 提到 8。修正前的一轮是 B0 6/6、B2 2/6——
B2 失败的原因是执行器看不到要改的文件，不是验证机制无效。

第三轮在 32/36 次时接口返回 502 中断，因此 `rename_chain_7` / `rename_chain_8`
每个配置只拿到 2 次样本（其余任务各 3 次）：

| task | config | runs |
|---|---|---|
| rename_chain_3 | B0 | 3 |
| rename_chain_3 | B2 | 3 |
| rename_chain_4 | B0 | 3 |
| rename_chain_4 | B2 | 3 |
| rename_chain_5 | B0 | 3 |
| rename_chain_5 | B2 | 3 |
| rename_chain_6 | B0 | 3 |
| rename_chain_6 | B2 | 3 |
| rename_chain_7 | B0 | 2 |
| rename_chain_7 | B2 | 2 |
| rename_chain_8 | B0 | 2 |
| rename_chain_8 | B2 | 2 |

> 与 `docs/bench.md` 第八节的长链任务数据同源。
