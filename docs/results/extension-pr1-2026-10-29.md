# Extension v1：M0 → PR 1 阶段验收

2026-10-29。**PR 1 验收通过，按用户“阶段验收通过后停止”的指令停在本阶段。PR 2–4、M5 尚未执行，全计划尚未完成。**

生成来源：同一冻结目录的 `report.json`、`coverage.json`、原始 Trace、JUnit、独立检查及 `audit_evidence.py`。范围依据 [主计划 v1](../extension-roadmap.md)，接口与兼容迁移见 [Hook 契约](../extension-hooks.md)。

## 源码与改动范围

- 源码 SHA-256：`fc97ade3829332219763a64f002e25dcf409d8f1e806ca0c84a3f502be1943f7`。
- Git HEAD：`a49569d15bcb87d160d2bf9f638e838b410d8179`，工作树有原有未提交/未跟踪改动；HEAD 不是本次实现快照。未创建提交或远端 PR，没有打包提交用户的其他改动。
- 证据根目录：`benchmark-results/extension-v1/pr1-fc97ade382933221/`，本机保留、不入 Git。`source_manifest.json` 逐文件列 hash，`source.tar.gz` 保存受控源码、测试、门禁文档与固定夹具。依明确根目录/后缀及固定文件清单收集，不归档 .env、凭据、缓存、虚拟环境或未筛选工作树。
- 本阶段修改 domain/ports 契约、现有 HookRegistry、Loop、Tool Runtime、两条组装入口，以及直接相关测试/示例/文档。新增 `FinishDecision`、受限 `ControlledCaller`、可选 `ToolObservation.effective_call`；未新增第二 Loop、插件体系或 CLI 产品交互。

原 M0 的真实结果保留在 [M0 报告](extension-m0-2026-10-29.md) 指向的目录。本轮发现其源码归档没有覆盖完整门禁所需文档/夹具，已由本次完整快照补齐并实测独立解包复现。此处是**当前版本复验**；没有把它描述成重新实测旧 M0。六题与旧 M0 的一致性比较读取历史证据，另存 `compatibility.json`。

## 固定交付与门禁

新旧接口保留旧调用签名与排序；V2 按注册顺序执行。旧结束回调仍是通知，新的完成决策单独处理 allow/continue/fail。策略错误关闭动作；新观察错误独立审计，不改真实结果。每个已开始步骤和运行只收束一次。新旧请求变换都保留系统/任务约束，工具改写重新经过查找、Schema 和权限。

完成检查使用原 run_id、步数、Token、工具和墙钟预算；受控 I/O 生成唯一 call_id，记录 assistant/tool 配对及实际改写信息。额度耗尽和未知用量停止后续动作，原始消耗不归零。能力在当前提议及创建任务中有效，禁止递归、后台使用、结束通知启动工具，以及回调结束后继续使用。local-first 与重复动作状态按实际调用更新，失败编辑不能被 allow 放行。

本阶段仅使用 fake checker，未实现 Skills、真实 Verification、Memory；关闭可选扩展的路径保持兼容。

| 检查 | 当前工作树 | 解包后独立目录 |
| --- | --- | --- |
| `uv run ruff format --check .` | 231 文件通过 | 通过 |
| `uv run ruff check .` | 通过 | 通过 |
| `uv run mypy src tests` | 129 文件通过 | 129 文件通过 |
| `uv run pytest` | **665 passed** | **665 passed** |

独立目录由本次 source.tar.gz 解包，使用 Python 3.12.14 与 `uv sync --locked --all-groups --offline` 安装。完整命令及退出码见 `reproduction-final-checks.json`，日志为 `quality/*` 和 `reproduction-final-*.log`。门禁/定向/复现是不同执行范围，不相加为“功能数量”。

## 覆盖矩阵与实际证据

本阶段 9 项扩展/Context 必测项加 2 项安全探针，**通过 11 / 已检查 11 / 本阶段声明机会 11，覆盖率 100%**。全计划仍为**已检查 11 / 声明 28，证据覆盖率 39.3%**；其余 17 项保持未测，属于后续阶段。完整表见 [覆盖 JSON](extension-pr1-coverage-2026-10-29.json)。

| ID | 已检查的实际行为 | 独立检查记录 |
| --- | --- | --- |
| H01 | 七分支空 Hook 配对；六题 Baseline/A 与 M0 历史结果、请求/动作、Token、Trace、产物一致 | [check.json](../../benchmark-results/extension-v1/pr1-fc97ade382933221/H01/0/check.json) |
| H02 | 依赖、重复服务、缺依赖/环、启动失败与逆序回滚；V2 注册释放一次 | [check.json](../../benchmark-results/extension-v1/pr1-fc97ade382933221/H02/0/check.json) |
| H03 | 旧 priority/owner 排序、新注册顺序、三扩展混用清单；三次复查及幂等卸载 | [check.json](../../benchmark-results/extension-v1/pr1-fc97ade382933221/H03/0/check.json) |
| H04 | 深复制事实、工具集合/call_id/系统任务保护；旧路径同样保护；实际改写信息驱动状态跟踪 | [check.json](../../benchmark-results/extension-v1/pr1-fc97ade382933221/H04/0/check.json) |
| H05 | Schema、权限、真实越界路径拒绝及合法改写；外部哨兵保持原样 | [check.json](../../benchmark-results/extension-v1/pr1-fc97ade382933221/H05/0/check.json) |
| H06 | 七阶段异常/超时/取消、全部工具结果状态、预算/未知用量、末步期限、清理与无重复副作用 | [check.json](../../benchmark-results/extension-v1/pr1-fc97ade382933221/H06/0/check.json) |
| H07 | allow/continue/fail、首拒绝短路、下一提议重查；同 run、原预算及受控计账 | [check.json](../../benchmark-results/extension-v1/pr1-fc97ade382933221/H07/0/check.json) |
| H08 | 配置与嵌入两条组装路径；第三方审计/注入示例和插件卸载 | [check.json](../../benchmark-results/extension-v1/pr1-fc97ade382933221/H08/0/check.json) |
| C01 | 六基础 Context 与 budget 分支回归；策略源码及构造函数 AST 与 M0 相同 | [check.json](../../benchmark-results/extension-v1/pr1-fc97ade382933221/C01/0/check.json) |
| E01 | 原生禁网容器隔离、危险命令/越界拒绝和合法写入；真实文件与网络检查 | [check.json](../../benchmark-results/extension-v1/pr1-fc97ade382933221/E01/0/check.json) |
| E02 | 真实父子进程超时/取消；停止与移除容器；当前插件/Hook 收束检查 | [check.json](../../benchmark-results/extension-v1/pr1-fc97ade382933221/E02/0/check.json) |

- 183 项定向检查全部通过，其中扩展模块 75 个参数化 case。映射在 `benchmarks/extension_v1/pr1_cases.json`，判据版本 `pr1-contracts-v1`；每个 test check 记录 node ID、判据文件 hash、源码身份和受控夹具 hash。
- 82 次实际契约 Loop 留有原始 trace.jsonl 与绑定 run_id 的 sidecar；纯组件 case 仅用 test ID，不伪造 Loop。检查 UTC/schema、身份、单调 step、唯一终态、原始 Trace hash 和两个预声明的异常文本 canary。该探针范围不能扩张为任意秘密的零泄漏结论。
- 六题 × Baseline/A 两组共 12/12 通过运行外独立判据。`offline/<group>/<case>/0/` 保存终态、配置、预算、已知脚本 Token、产物 hash 和独立结果。隐藏判据在 Agent 退出后覆盖并执行；参考解用于固定脚本输出及运行外参考检查，没有生成模型评测。
- 六个参考解通过、六个缺陷实现失败，共 12/12 区分检查符合预期。原生 arm64 镜像 digest、1 CPU/1 GiB/禁网配置固定；记录在 `images/images.json` 与 `fixture-gates.json`。
- E01：宿主哨兵前后 hash 相同、容器不可见该路径；公网连接失败；合法写入成功；危险命令和越界 cwd 被拒。
- E02：真实父子进程分别被超时/取消停止，docker inspect/top 与移除检查确认无容器内残留进程。证据是实际副作用检查，不以 mock 代替 OS 探针。

原生 local adapter 本身不是 OS 隔离边界；上述 OS 结论限定于当前原生容器及探针。Python 插件是受信、协作式代码，Hook 期限不等于对恶意同步阻塞插件的 OS 隔离。

## 开销、兼容与结论边界

内存模型/工具、无磁盘 Trace handler，计时仅含 AgentLoop.run；每批预热 20 次/组、测量 200 次/组，共 3 批，样本内交替先后顺序。P95 用 nearest-rank，计算每批 A − Baseline 后取中位数。纯审计组覆盖七阶段，模型请求/结果不变。

本轮增量中位数 **0.579 ms**，冻结门槛 **5 ms**，通过；原始 1,200 个测量值、各批 P95、平台与负载保存在 `overhead.json`。仅支持该环境下的开销门禁，不宣称真实模型延迟或功能收益。

真实 DeepSeek 队列 **0/90 已运行，90 行未运行**；没有调用真实模型，不计算货币费用。Skills/Verification/Memory 收益与模型能力均保持未知，后续 M5 才执行冻结配对，不从脚本结果宣传机制收益。三个离线示例均退出 0：minimal_demo、custom_runtime、extension_demo；日志在根目录。

## 审计与复现

阶段预审材料完整保留。第一轮发现旧回调缺审计、结束回调可直接调工具及异常文本泄漏；第二轮补旧请求约束、末步预算优先及实际改写驱动状态投影。各缺陷均有回归先红后绿。早期目录的预审自动结果不能替代本次最终快照；当前结论来自本目录最终独立检查。

复现时先逐文件核对 source_manifest.json。源码不同时，将 source.tar.gz 解到**新的**受控目录，从该目录运行四项门禁和三个示例；不要覆盖现有工作树。复制 collector 脚本、manifest、source_manifest、images 配置到新的证据根目录后运行，禁止复用历史输出目录：

```bash
uv run pytest -q tests/test_extension_hooks.py tests/test_runtime.py tests/test_composition.py tests/test_harness_extensions.py tests/test_harness_safety.py tests/test_context.py tests/test_context_retention.py tests/test_security.py --extension-evidence=<新证据根>/contracts --junitxml=<新证据根>/contracts.xml
uv run python <新证据根>/capture/baseline.py
uv run python <新证据根>/audit/baseline.py
uv run python <新证据根>/probes.py
uv run python <新证据根>/overhead.py
uv run python <新证据根>/audit_evidence.py
```

collector/scorer 都保存在证据目录，脚本自身有 hash，复用现有 BenchmarkRunner、DockerSandbox、指标与 pytest，没有新增阶段专用产品 runner。只读完整性检查使用本目录 `verify_seal.py`；派生结果另存新目录，不改历史 Trace。

验收后按指令停在 PR 1。允许讨论 PR 2 的下一步，但本次没有开始 PR 2–4 或 M5；整项 M0–M5 目标仍有后续工作。
