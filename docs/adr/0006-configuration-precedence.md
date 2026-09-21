# ADR 0006：分层配置与来源可追溯

- 状态：Accepted
- 日期：2026-09-21

## 背景

Coding Agent 的配置项横跨两件事：**预算与行为**（步数、Token、工具调用、墙钟）和**权限**（允许哪些工具、哪些命令、能不能联网）。这两类的风险等级完全不同：把 `max_steps` 配错只是多花点钱，把 `allow_shell` 配错可能执行破坏性命令。

因此配置系统有两个要求：一是优先级必须可预测，二是**生效值必须可追溯到来源**——"为什么这次运行能跑 shell"应该是一个能直接回答的问题，而不是靠翻环境变量猜。

## 决策

### 优先级

```text
CLI 参数  >  环境变量（MINIDSH_*）  >  minidsh.toml  >  安全默认值
```

- 环境变量统一以 `MINIDSH_` 为前缀；非前缀变量被忽略，未知的 `MINIDSH_*` 变量也不会静默生效。
- 配置文件读取 `[minidsh]` 表；也接受无 section 的扁平结构。
- 类型强制转换显式且有界：整数/浮点字段解析失败抛 `ConfigError`，不静默回退到默认值。
- 显式的 `None`（如 CLI 未提供的可选参数）不参与覆盖，避免"未指定"被误当作"显式清空"。

### 来源可追溯

`resolve_config` 返回 `ResolvedConfig(options, budget, sources)`，其中 `sources` 记录**每个生效字段来自哪一层**：

```bash
$ uv run mini-dsh config
max_steps = 12        (config)
max_tokens = 200000   (default)
allow_shell = true    (cli)
...
```

这使得"权限从哪来"成为可审计事实，也是排查"本地跑得通、CI 跑不通"这类问题的第一手材料。

### 安全默认值

- 缺失配置时**不静默启用高权限能力**：默认只注册 Read、Search 与只读 Git；Write 与 Shell 必须显式开启。
- `denied_tools` 作为独立层存在，且 deny 优先——即使某个能力被授予，显式拒绝仍然生效。
- 预算四层（`max_steps` / `max_tokens` / `max_tool_calls` / `max_duration_seconds`）相互独立，任一耗尽都会安全终止并记录原因。

### 兼容别名

`max_seconds` 作为 `max_duration_seconds` 的配置文件别名保留（`_FILE_ALIASES`），在读取文件层归一化，而不是靠 pydantic 的 `validation_alias`——因为模型使用 `extra="forbid"`，别名必须在进入校验前处理，否则会被判为非法字段。

## 替代方案

- **单一来源（只读环境变量）**：简单，但无法为不同任务集保存配置，也无法在 CLI 上做一次性覆盖，拒绝。
- **只读配置文件**：可版本化，但 CI 与临时实验需要覆盖，拒绝。
- **静默回退到默认值**：看起来健壮，实则掩盖配置错误（把拼错的字段当成默认值），拒绝；解析失败一律显式报错。
- **不记录来源**：省几行代码，但让权限问题难以定位，拒绝。

## 后果

- `mini-dsh config` 成为排障与审计的入口，`sources` 直接暴露优先级结果。
- 未知或越界字段被拒绝，配置错误在启动阶段暴露而不是运行中途。
- 代价：字段需要在 dataclass、TOML 解析、环境变量映射、CLI 覆盖四处登记，新增配置项有一定样板代码；用统一的 `_INT_FIELDS` / `_FLOAT_FIELDS` 集合与集中式字段表约束，避免四处漂移。
