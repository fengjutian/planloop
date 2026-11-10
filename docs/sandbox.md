# 沙箱：现状、选型与本次实现

> 状态（2026-11）：**简单版本已实现**（`sandbox.py`）。
> `host`（环境变量脱敏）/ `none`（拒绝执行）/ `docker`（无网络一次性容器）。
> 强隔离（MicroVM、eBPF 网络策略、凭据代理）保留为下一步。

## 一、先分清四个层次

| 层 | 作用 | 现状 |
|---|---|---|
| **git 提交点** | 出错能退回去 | ✅ P1 |
| **策略**（`policy.py`） | 人知情、可拒绝 | ✅ P5 |
| **沙箱** | 就算执行了也伤不到外面 | ⚠️ 只有弱版本 |
| **网络出口** | 不把数据带出去 | ❌ |

四者不是替代关系。策略拦不住"代码自己删文件"，沙箱拦不住"用户点了同意"，git 拦不住"密钥被读走"。

## 二、市面方案

| 方案 | 隔离强度 | 冷启动 | 门槛 | 适用 |
|---|---|---|---|---|
| **Docker / 容器** | 中（共享内核） | ~200ms | 低 | 本机开发、CI |
| **CubeSandbox**（腾讯） | 高（KVM MicroVM，独立内核） | 宣称 <60ms | 需 Linux + KVM | 云端、多租户 |
| **E2B** | 高（Firecracker 类） | 快 | 商业/自托管 | 托管沙箱 |
| **gVisor** | 中高（用户态内核） | 中 | 中 | 不信任容器隔离时 |
| **WSL2 + bwrap/nsjail** | 中 | 快 | 需 WSL | Linux 开发者 |
| **Windows Job Object / AppContainer** | 中 | 快 | 需 ctypes | 不想装 Docker |
| **只读挂载 + 临时副本** | 低 | 极快 | 极低 | 防误删，不防外泄 |

本机是 Windows，没有 KVM 直通，**CubeSandbox 跑不起来**；现实选项是 Docker 或"只读副本"。

## 三、腾讯 CubeSandbox

`TencentCloud/CubeSandbox`，2026 年 4 月 v0.1.0 开源，定位"给 AI Agent 跑不可信代码"。

| 能力 | 做法 | 对我们的意义 |
|---|---|---|
| 隔离 | RustVMM + KVM，每沙箱一个独立 guest kernel | 容器逃逸打不到宿主 |
| 启动 | 快照池化 + 内核 CoW，宣称 <60ms；单沙箱 <5MB 底噪 | 每节点新开沙箱才现实 |
| 接口 | 兼容 **E2B SDK**，换一个环境变量即可切换 | 我们只要按 E2B 语义写后端 |
| 网络 | eBPF 隔离 + L7 反向代理，**凭据不进沙箱** | 补齐"读 `.env` 再外发"这个洞 |
| 状态 | 增量快照、可回退、可分叉多条平行分支 | 比"每节点一个 commit"细，适合探索型任务 |
| 存储 | Volume 插件（S3 / host mount） | 挂载方式可换 |
| 部署 | 单机 / 多机（CubeMaster）/ ARM64 原生 | 未来并行调度可直接用 |

**注意**：以上数字与能力来自其官方文档与宣传材料，尚未在本项目实测。选型前应自行压测。

其他腾讯体系里的"沙箱"要区分：蓝鲸的沙箱是 PaaS 应用部署环境，TencentOS/OpenCloudOS 是内核与发行版，Agent Runtime 是商业产品封装——都不是代码执行沙箱。

## 四、本次实现的简单版本

```python
planloop run spec.json -w ./repo --sandbox host   # 默认
planloop run spec.json -w ./repo --sandbox none   # 只允许验证，不允许执行
planloop run spec.json -w ./repo --sandbox docker # 一次性容器，无网络
```

### `HostSandbox`（默认）

- **环境变量脱敏**：名字匹配 `API_KEY / SECRET / TOKEN / PASSWORD / CREDENTIAL / AWS_* / OPENAI_* …` 的变量不传给子进程；
- 保留 `PATH / HOME / TEMP / SYSTEMROOT / PYTHONPATH` 等运行必需项；
- 每次运行记录被删掉的变量名，可在审计里查。

```python
$ MY_API_KEY=sk-xxx  →  子进程里 os.environ.get("MY_API_KEY") == None
```

这条拦住的是**最常见的外泄路径**：模型让代码 `print(os.environ)` 或把它 POST 出去。

### `NoExecSandbox`

任何 `run` 命令直接返回 `exit=126`，只允许 `read/write` 与验收。适合"审阅一遍代码"或"只跑只读检查"的场景。

### `DockerSandbox`

```
docker run --rm --network none --memory 1g --pids-limit 256 \
           -v <workspace>:/work -w /work <image> sh -lc "<cmd>"
```

只有工作区被挂进去，宿主其它路径不可见；无网络。镜像默认 `python:3.11-slim`，需要按仓库工具链替换。

若 Docker 不存在，`--sandbox docker` **直接报错退出**，不会静默退回 host。

## 五、能挡什么，挡不住什么

| 场景 | host | docker |
|---|---|---|
| `print(os.environ)` 拿密钥 | ✅ 挡住 | ✅ |
| 读 `~/.ssh/id_rsa` | ❌ 能读 | ✅ 不可见 |
| 删掉工作区外的文件 | ❌ | ✅ 不可见 |
| `rm -rf .` 删掉工作区 | ❌（靠 git 提交点恢复） | ❌（挂载可写） |
| 把数据 POST 出去 | ❌ | ✅ 无网络 |

**结论：`host` 不是隔离边界，只是"少泄露一点"。真要跑不可信代码，用 `docker`，或者上 CubeSandbox 这类 MicroVM。**

## 六、为什么这次不做强隔离

1. **本机跑不了**：CubeSandbox 需要 Linux + KVM；这台是 Windows。
2. **Docker 已经够用**：单机开发场景下，"只挂工作区 + 无网络"能挡住绝大多数事故。
3. **接口比实现重要**：`Sandbox` 协议（`run/write/read`）定下来，将来换后端不用动调度器。

## 七、后续

| 阶段 | 内容 | 触发条件 |
|---|---|---|
| S1 | `E2B` 兼容后端（可指向 CubeSandbox / E2B） | 有 Linux 机器或云环境 |
| S2 | 快照式回滚（沙箱侧），替代部分 git checkpoint | 需要"试两条路径再选" |
| S3 | 网络出口白名单 + 凭据代理 | 真的要跑联网的 agent |

**S1–S3 都不该在本机仓促实现**：没有真实 Linux 宿主，写出来也验证不了。
