# 陷阱题（文档约定 / 归档副本）：两个机制都没触发

> 由 `.demo/trap-bench.log` 汇总。题目：`division_convention`（约定写在 `calc/README.md`）、
> `legacy_copy`（可见测试指向归档副本）。判据是 held-out 的，模型看不见断言。
>
> **结论：这两个机制对 MiniMax-M3.1-Flash 不成立。** 12 格全部成功，
> B0 一次没挂——模型把 README 里的约定读进去、把两份拷贝都改了。
> 所以它们**不能**用来制造失败，只能作为"模型比预期更仔细"的记录。
# Benchmark: B0 / B1 / B2

- repetitions per cell: **2**
- per-attempt success probability of the scripted model: **0.6**
- seed: 0
- sampling temperature: **0.2**

> 没有真实模型凭据时，这些数字验证的是 **harness 与度量管线**，不是模型能力。
> 成功由独立 oracle（跑测试）判定，不由被测配置自评。

| config | success_rate | mean_tokens | runs |
|---|---|---|---|
| B0 | 100% | 18544 | 4 |
| B1 | 100% | 54547 | 4 |
| B2 | 100% | 6720 | 4 |

## 按任务

| task | config | success | tokens |
|---|---|---|---|
| division_convention | B0 | yes | 20104 |
| division_convention | B1 | yes | 47767 |
| division_convention | B2 | yes | 8219 |
| legacy_copy | B0 | yes | 17584 |
| legacy_copy | B1 | yes | 55746 |
| legacy_copy | B2 | yes | 3039 |
| division_convention | B0 | yes | 16435 |
| division_convention | B1 | yes | 60219 |
| division_convention | B2 | yes | 7202 |
| legacy_copy | B0 | yes | 20055 |
| legacy_copy | B1 | yes | 54456 |
| legacy_copy | B2 | yes | 8421 |
