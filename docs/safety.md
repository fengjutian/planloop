# 高危动作：拦截与确认

> 状态：**已实现**（P5）。`policy.py` 规则裁决、`approvals.py` 一次性批准、工具层强制、`planloop approvals` CLI。

## 一、为什么不能"弹窗问一下"

在 CI、后台任务、凌晨无人值守时，"问用户"只有两个结局：**等同于放行**，或者**永远卡死**。两者都不能接受。

所以：

```
命中 confirm 规则
   ├─ 交互终端  → 可以当场问（尚未实现，见文末）
   └─ 无人值守  → 拒绝执行 → 写 pending 批准 → 运行停下
                        ↓
              人执行 planloop approvals approve <token>
                        ↓
                  planloop run --resume
```

批准是**磁盘上的记录**，不是内存里的一个 yes。所以它可审计、可追溯、可在另一个进程里补签。

## 二、三级裁决

| 级别 | 含义 | 例子 |
|---|---|---|
| `allow` | 直接执行 | `python -m unittest`、`git status`、`npm test` |
| `confirm` | 需一次性批准 | `rm -rf`、`git push --force`、`git reset --hard`、`npm publish`、`pip install`、`sudo`、`curl \| sh` |
| `deny` | 永不允许，不可批准 | `rm -rf /`、`shutdown`、`mkfs`、`DROP DATABASE`、凭据外传 |

判定是**确定性的正则规则**，每条都带规则名与理由。模型不参与裁决——它对自身风险的判断不可审计，且会抖动。

```python
$ planloop ...        # 内部
policy.assess_command("git push --force origin main")
# Assessment(level="confirm", rule="git_force_push", reason="rewrites remote history")
```

**fail-closed**：看不懂的 shell 结构（`$(...)`、反引号、`bash -c`）一律 `confirm`，不是 `allow`。会有噪音，但噪音是诚实的选择。

## 三、写文件也会被拦

命令安全不代表安全。写这些路径等于**给下一次运行埋雷**：

| 路径 | 为什么 |
|---|---|
| `.git/hooks/*` | 下次 commit 自动执行 |
| `.github/workflows/*` | 推到远端后自动执行 |
| `.git/config` | 改仓库行为 |

所以 `Tools.write` 走同一套闸门，规则名分别是 `git_hooks` / `ci_config` / `git_config`。

## 四、批准是什么

```json
{
  "token": "ap-9901fca2",
  "action": "run",
  "target": "python -c \"import shutil; shutil.rmtree('scratch')\"",
  "reason": "runs inline code that can touch the system",
  "rule": "inline_code",
  "run_id": "r-policy",
  "node_id": "T1",
  "status": "approved",
  "expires_at": 1794364042,
  "max_uses": 1,
  "uses": 0
}
```

四重绑定，缺一不可：

| 绑定 | 防止什么 |
|---|---|
| 目标哈希（`sha256("action\0target")[:16]`） | 批了 A 命令，拿它跑 B |
| `run_id` | 跨任务复用 |
| 过期时间（默认 24h） | "以前批过，永久有效" |
| `max_uses=1` | 一次批准当长期通行证 |

存储是 append-only 事件流（`request/approve/deny/consume`），状态靠重放得出——和系统其余部分一致：**不原地改状态**，所以"谁在什么时候批了什么"能活过崩溃。

## 五、实测流程

```
$ planloop run spec.json -w ./repo
status: blocked
blocked: high-risk action needs approval: runs inline code that can touch the system
  needed: 命令: python -c "import shutil; shutil.rmtree('scratch')"
          批准: planloop approvals approve ap-9901fca2 -w ./repo
          然后: planloop run --resume -w ./repo
scratch still there: True          ← 批准前什么都没执行

$ planloop approvals list -w ./repo
ap-9901fca2  [pending]  run: python -c "import shutil; shutil.rmtree('scratch')"
    why: runs inline code that can touch the system (rule inline_code)
    run: r-policy  node: T1  uses 0/1

$ planloop approvals approve ap-9901fca2 -w ./repo --by charles
approved ap-9901fca2
next: planloop run --resume -w ./repo

$ planloop run --resume -w ./repo
status: succeeded
  node T1: done (3 steps)
scratch gone: True
```

`blocked` 报告里直接给出**下一步命令**，不让人猜。

## 六、审计里能看到

```
- 10:28:20 T1 开始（第 1 次尝试）
- 10:28:20 [!] 等待批准 ap-9901fca2: python -c "..."
- 10:28:20 结束: blocked
- 10:28:20 续跑，已完成节点 []
```

批准记录本身也在 `.planloop/approvals.jsonl` 里，含 `approved_by`。

## 七、这套东西解决不了什么

| 场景 | 现状 |
|---|---|
| 合法命令的副作用（`npm test` 触发 postinstall 删文件） | 拦不住，只能靠沙箱 |
| 高度混淆的载荷（base64 + `eval`） | `eval`/`bash -c` 会被 confirm，但拼装后的语义无法静态判定 |
| 用户疲劳点一路 y | 一次性令牌 + 明确后果描述能缓解，不能根治 |
| 验收命令本身高危 | 会返回失败判定并让运行停下询问，而不是偷偷执行 |

**确认机制是第二道防线，沙箱才是第一道。** 两者关系：沙箱限制爆炸半径，确认保证人类知情。

## 八、明确没做的

- **交互式终端提示**：现在一律 fail-closed，需要人在另一个命令里批准。TTY 内联确认是后续优化。
- **策略白名单配置文件**：`CommandPolicy(allow_exact=..., allow_prefix=...)` 已支持，但还没有 `.planloop/policy.toml` 加载器。
- **按仓库自定义规则**：同上。
