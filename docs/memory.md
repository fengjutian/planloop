# 错误记忆：让 AI 不再犯同样的错

> 本篇只讲**负向记忆**（错误与护栏）。正向记忆（事实、程序、决策、事件）见 [记忆系统](memory-system.md)。

## 一、为什么多数"记忆"没用

大多数实现把错误存成一句自然语言提醒："注意 X 模块有坑"。它有三个致命问题：

| 问题 | 后果 |
|---|---|
| 检索靠语义相似度 | 该用时召不回，不该用时乱召回 |
| 只有提示，没有约束 | 模型可以不理，而且会被后续上下文冲淡 |
| 没有生命周期 | 错误记忆永久生效，误杀越来越多 |

所以这里用**双通道**：

```
lesson = 签名 + 触发条件 + 原因 + 修法 + 护栏(可选) + 证据 + 信任度
           │        │                              │
           │        └── 可判定的匹配条件            └── 可机器执行的验收，模型不听也拦得住
           └── 归一化后的哈希，用来判断"是不是同一个错误"
```

- **软通道**：lesson 进入该节点的上下文切片，帮模型别走老路；
- **硬通道**：lesson 自带的护栏在后续任务里被注入计划，成为必须通过的验收项。

## 二、签名：什么样的"同一个错误"

对报错文本做归一化，再做哈希：

```python
VOLATILE = [
    (r"[A-Za-z]:\\[^\s:'\"]+", "<path>"),      # 绝对路径
    (r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\b", "<ts>"),  # 时间戳
    (r"\b0x[0-9a-fA-F]{4,}\b", "<hex>"),
    (r":\d+:\d+:", ":<l>:<c>:"),               # 行列号
    (r"\b\d{6,}\b", "<id>"),
]
```

但**选哪一行**比归一化更重要。这里踩过一个真实的坑：

> 最初取"最后一行匹配 Error|FAILED 的内容"，于是 pytest 的
> `FAILED (failures=1)` 成了签名。结果两个完全不同的错误被合并成一条记忆，
> 复现率 100%，而实际上什么都没记住。

现在的做法是**按信息量排序**取行：

```
1. 具体的异常行        ModuleNotFoundError: No module named 'shop.helpers'
2. FAIL: <测试名>      FAIL: test_total_applies_discount
3. assert 行           AssertionError: 290 != 270
4. 兜底：最后一行
```

**通用性来自触发条件，而不是把签名做模糊。** 这一点是刻意的：签名太泛 → 误合并；太窄 → 不复发。所以签名保持精确，广度交给 trigger。

## 三、触发条件：可判定，不靠相似度

```json
{"files": ["shop/*.py"], "goals": ["pricing|export"], "commands": [], "error_patterns": []}
```

语义：**每个非空分区都必须命中，分区内任一命中即可**。

- `files`：fnmatch 匹配工作区文件；
- `goals`：正则匹配任务目标；
- `commands`：正则匹配执行过的命令；
- `error_patterns`：正则匹配失败文本。

编译期只有 goal 和文件列表，所以护栏注入依赖 `files`/`goals`；`error_patterns` 只在执行期（已有报错文本时）参与。

## 四、护栏准入：必须现在就能跑通

一条 lesson 声称"以后要检查 X"是廉价的。所以入库前会把护栏**实际执行一次**：

```
护栏在当前工作区跑通  → strength="guard"，trust=0.70
护栏跑不通            → 降级为 advisory，trust=0.45，护栏丢弃
```

这样不可能出现"幻觉护栏"：一个通不过的检查不会被写成未来的阻塞项。

## 五、信任度与退役

| 事件 | 影响 |
|---|---|
| 同一签名再次出现 | `recurrences += 1`，trust +0.15 |
| 护栏拦截成功 | `catches += 1`，trust +0.10 |
| 护栏在正确的工作上失败（误杀） | `guard_false_positives += 1`，trust −0.30 |
| trust < 0.20 | 状态置 `refuted`，降级为 advisory，不再阻塞 |

误杀的代价高于漏放：一条乱报警的护栏会让人开始无视所有护栏，整套机制随之失效。

## 六、数据结构

```json
{
  "id": "e019a37b14bc3",
  "signature": "ModuleNotFoundError: No module named 'shop.helpers'",
  "family": "ModuleNotFoundError: No module named '<s>'",
  "summary": "Imported a symbol from another module before that module provided it",
  "cause": "added the caller and the provider in separate steps",
  "fix": "create the providing module first, then run the whole suite",
  "trigger": {"files": ["shop/*.py"], "goals": ["pricing|export|shop"]},
  "guard": {"kind": "command", "spec": "python -m unittest discover -s tests -q", "must": "exit=0"},
  "strength": "guard",
  "trust": 0.85,
  "status": "active",
  "hits": 3, "prevented": 1, "catches": 1, "recurrences": 0,
  "evidence": ["ModuleNotFoundError: ...", "wrote shop/helpers.py"],
  "source": "model"
}
```

存储：一行一个 JSON（JSONL），位置默认 `.planloop/memory.jsonl`。

## 七、实测结果

见 `README` 的四组对照。核心数字：

| 组 | 有没有记忆 | 模型听不听 | 结束后全量测试 |
|---|---|---|---|
| B | 无 | — | **红（回归被静默留下）** |
| C | 有 | 不听 | 绿（护栏拦截 1 次） |
| D | 有 | 听 | 绿（0 次失败） |

B 组是这个实验的意义所在：**没有记忆时，"运行状态成功"和"仓库其实是好的"是两件事。**

## 八、已知弱点

1. **护栏的准入门槛只能证明"现在能跑通"，不能证明"以后不会误杀"。** 所以必须有误杀衰减。
2. **触发条件写得越宽，误命中越多。** 现在靠人工定的 `goals` 正则，还没有自动收敛机制。
3. **记忆会无限增长。** 目前没有 TTL 与容量上限（见 `docs/limits.md`）。
4. **跨项目污染。** 单文件存储不分项目作用域，"必须用 `python -m pytest`"会被带进 Rust 项目。建议按仓库隔离。
5. **签名只覆盖"有明确报错文本"的失败。** 需求理解错误、设计方向错误这类没有报错的错误，记忆帮不上忙。
6. **近似重复会并存。** 两个不同的报错文本可能来自同一个根因，按签名身份会存成两条 lesson。演示里就有这种情况（`e019…` 与 `e714…` 的内容几乎一致）。目前没有聚类合并机制，长期会让匹配结果变得冗长。
