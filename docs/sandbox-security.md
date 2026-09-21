# Sandbox 与权限模型

Coding Agent 的本质是**让不可信文本驱动有副作用的操作**。模型输出是不可信输入，仓库内容也是不可信输入（prompt injection 的常见载体）。Mini-dsh 的安全设计因此从一条原则出发：**默认拒绝，按运行授予最小权限，且授权是可审计的事实而不是隐式的约定。**

## 1. 威胁模型

我们防御四类攻击者：

| 攻击者 | 能力 | 典型目标 |
| --- | --- | --- |
| 不可信模型 | 产出任意 tool_call 参数 | 越权写文件、逃逸工作区、读取密钥、外联 |
| 被注入的仓库内容 | 在文件里埋指令 | 诱导模型执行危险命令、泄露 `.env` |
| 有缺陷的 Harness 代码 | 拼接错误、路径处理疏漏 | `..` 绕过、符号链接逃逸、Shell 注入 |
| 失控的子进程 | 派生孙进程、跑满资源 | 绕过超时、耗尽 CPU/内存 |

**明确不在范围内**：本地的 `LocalCommandRunner` 不做内核级隔离。一旦允许执行 `python` 之类的通用解释器，它理论上可以直接发起系统调用或网络访问。要抵御恶意代码必须用 `DockerCommandRunner` 或外层 VM，见第 6 节。

## 2. 六道防线

### 防线一：工作区路径约束（路径逃逸）

`WorkspaceSandbox.resolve` 是所有文件操作的唯一入口：

1. **拒绝绝对路径**——`/etc/passwd` 直接抛 `SandboxViolationError`。
2. **`resolve(strict=...)` 做真实路径规范化**，再 `relative_to(root)` 校验归属，因此 `a/b/../../..`、`nested/../../secret.txt` 全部被拦。
3. **不展开 `~`**——`~/secret` 被当作工作区内名为 `~` 的目录，而不是用户主目录。

写入走 `atomic_write`：先在**已校验的父目录**内 `mkstemp` 建临时文件，`fsync` 后 `replace` 原子替换；任何异常都会清理临时文件。父目录同样要过 `relative_to(root)`，所以「写到一个指向外部的符号链接目录」也会失败。

### 防线二：符号链接防逃逸

因为校验发生在 `resolve()` 之后，符号链接指向的**真实路径**才参与归属判断。工作区内建一个 `escape -> /outside` 的链接后，`read_text("escape/secret.txt")` 会被拒绝，因为真实路径落在工作区外。对文件链接与目录链接都成立，`atomic_write` 穿透符号链接父目录同样被拒。

### 防线三：命令策略（Shell 注入）

- **只接受 argv 数组，从不调用 shell**。`subprocess_exec` + argv 意味着 `;`、`|`、`$()`、反引号都是普通参数字符面量，没有解释器去执行它们。
- **deny 优先于 allow**。内置 denylist 含 `rm`、`sudo`、`ssh`、`scp`，显式授权也无法覆盖。
- **显式 allowlist**。默认 allowlist 为空，`python`、`bash`、`sh` 都不在默认集合里；每个可执行文件必须逐次授予。
- **空 argv 被拒**，避免退化调用。

### 防线四：网络默认全关

`NetworkPolicy` 默认 `frozenset()`，即 deny-all。授权以 `(host, protocol, port)` 三元组表达：

```python
NetworkRule(host="pypi.org", protocol="tcp", ports=frozenset({443}))
```

- `host` 必须是单个无空白 token；支持 `"*"` 通配。
- `protocol` 只允许 `tcp` / `udp`；`ports` 必须在 1–65535。
- `curl`、`wget`、`nc`、`netcat`、`telnet`、`ftp` 在策略未启用时一律拒绝，即使已加入 allowlist。
- Docker 后端据此在 `--network none` 与 `bridge` 之间切换。

### 防线五：密钥不进入 Context

`ensure_not_sensitive` 在读取前拦截：

| 类别 | 规则 |
| --- | --- |
| 文件名 | `.env`、`.env.*`、`.npmrc`、`.pypirc`、`credentials`、`credentials.json`、`id_rsa`、`id_ed25519` |
| 后缀 | `.key`、`.p12`、`.pem` |
| 目录 | 路径中任何一段为 `.ssh` |

`search` 工具同样跳过这些文件，因此密钥不会经由检索结果进入模型 Context。错误消息只包含相对路径，**不回显文件内容**。

子进程环境变量走 allowlist，默认只传 `HOME`、`LANG`、`LC_ALL`、`PATH`、`TMPDIR`、`TZ`——`MINIDSH_API_KEY` 之类不会被继承。

Trace 落盘前二次脱敏：`api_key` / `token` / `password` / `secret` 等键、`Authorization: Bearer ...` 头、以及 `key=value` 形式的赋值都会被替换，测试断言原文不出现在 JSONL 中。

### 防线六：进程与超时

- 子进程以 `start_new_session=True` 启动，独立进程组。
- 超时后在 POSIX 上用 `os.killpg(pid, SIGKILL)` **杀掉整个进程组**，因此孙进程不会残留（测试用孙进程写标记文件的方式验证）。
- 工作目录必须 `resolve(must_exist=True)` 且位于工作区内、且是目录。

Docker 后端额外收紧：

```text
--rm --network none --read-only --cap-drop ALL
--security-opt no-new-privileges
--pids-limit 256 --memory 2g --cpus 2
--tmpfs /tmp:rw,noexec,nosuid,size=256m
--volume <workspace>:/workspace:rw
```

根文件系统只读，只有 workspace 可写，`/tmp` 以 `noexec` 挂载，全部 Linux capability 被丢弃。

## 3. 工具流水线中的位置

```text
Schema 校验 → 参数规范化 → 权限判断 → 执行 → 结构化 Observation
                │              │
                │              └─ PermissionPolicy（能力 + 工具 denylist，deny 优先）
                └─ WorkspaceSandbox.resolve（路径归属）
```

权限判断发生在执行之前，被拒绝的调用**不会启动任何进程或触碰任何文件**。拒绝会产出一条 `tool_call_completed` 事件（状态为拒绝、带错误码），因此 `permission_denied` / `unsafe_blocked` 是真实可统计的指标——`stress` 套件里 `denied=1, blocked=4` 就是这条链路的结果，而不是日志里的字符串。

## 4. 攻击测试

`tests/test_security.py` 覆盖六类攻击，共 36 个用例：

| # | 类别 | 用例 |
| --- | --- | --- |
| 23 | 路径逃逸 | `../secret.txt`、`nested/../../secret.txt`、`./../secret.txt`、`a/b/../../../secret.txt`、绝对路径 `/etc/passwd`、`~/` 不展开、Read/Write 工具层的逃逸拒绝 |
| 24 | 符号链接逃逸 | 目录链接逃逸、文件链接逃逸、经符号链接父目录的原子写拒绝 |
| 25 | Shell 注入 | 元字符以字面量传递、`sh -c` 被策略拒绝、解释器不在隐式 allowlist |
| — | 网络默认拒绝 | 无规则时网络命令被拒、`NetworkRule` 校验与匹配、策略默认 deny-all |
| 26 | 密钥泄露 | `.env` / `id_rsa` / `credentials.json` / `.pem` / `.ssh` 不可读、search 跳过、错误消息不回显内容、子进程环境 allowlist、Trace 脱敏 |
| 27 | 进程与超时 | 超时后进程组清理（孙进程标记文件不出现）、工作目录逃逸拒绝 |

补充的命令策略硬化用例：deny 覆盖 allow、空 argv 拒绝。

## 5. 已验证的边界

- **已验证**：路径归属（含符号链接与大小写归一）、Shell 元字符不可执行、网络默认 deny-all、密钥文件不可读不可检索、超时清理整个进程组、Trace 无密钥。
- **未验证（且不声称）**：本地模式下对恶意二进制的隔离。allowlist 里的 `python` 一旦获得授权，就能 `os.system("curl ...")`。策略层拦的是**Harness 主动发起的调用**，拦不住解释器内部的自发行为。
- **Docker 后端的边界**依赖宿主机 Docker 的正确安装与镜像供应链。镜像应固定到 digest 并由部署方审计；Mini-dsh 只负责生成约束参数。

## 6. 使用建议

| 场景 | 配置 |
| --- | --- |
| 可信仓库、只读分析 | 默认（仅 Read/Search/只读 Git），不开 write/shell |
| 可信仓库、需要改代码与跑测试 | `--allow-write --allow-shell --allow-command python` |
| 不可信仓库或不可信模型 | `--sandbox-backend docker --docker-image <pinned@sha256:...>`，且**不要**开 `--allow-network` |
| 需要联网装依赖 | 显式 `NetworkRule`，限定 host + 443，不要用 `*` |

原则：能不开就不开，能用容器就用容器，网络永远最后考虑。
