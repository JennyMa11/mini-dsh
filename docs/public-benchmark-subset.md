# 小型公开基准子集 v1

本方案因大型镜像和 amd64 仿真资源需求，已从本机默认评测中撤下。
本机改用 [轻量公开修复评测](lightweight-public-benchmark.md)，保留本清单与诊断供追溯。

选择一套 SWE-bench Pro V2 子集，共 12 个任务，覆盖其全部 11 个仓库。
Python 4 题、Go 4 题、TypeScript 3 题、JavaScript 1 题。首轮每题运行一次，
每题 75,000 Token，单配置整轮名义预算 900,000 Token；两个 Harness 配置的
配对比较合计名义预算 1,800,000 Token。费用不作为评估指标。

题目清单是人工按规格和能力范围选择的开发评测子集，尚未使用 Mini-dsh 试跑成绩筛题。
它优先保证仓库和任务范围的多样性，不按上游仓库题量比例采样。
上游统一的 `medium` 标注不足以区分实际难度，因此不声称各题难度相同。

**固定来源与题目**

[selection.json](../benchmarks/public_swe/v1/selection.json) 固定完整任务 ID、仓库、
初始提交、能力标签、预算、上游任务树 Git hash，以及规格、配置和 Dockerfile 的 SHA-256。
能力标签由本项目按题目规格整理，不是上游官方类别。
上游提交固定为 `66f92766bba642462d4bbe5479e83f91f9211862`。
已下载核对 12 个完整任务树（120 个文件），包括官方验证器和参考补丁。
参考/空补丁正在做环境门禁验证；模型成绩仅以独立运行报告为准。

| 本地 ID | 仓库 | 主要覆盖 |
| --- | --- | --- |
| `nodebb_topic_order` | NodeBB/NodeBB | 排序与接口行为 |
| `ansible_atomic_permissions` | ansible/ansible | 原子写入、文件权限与安全相关修复 |
| `element_admin_actions` | element-hq/element-web | 前端异步状态、重复操作 |
| `flipt_polling_lifecycle` | flipt-io/flipt | 后台轮询、资源释放、跨文件修改 |
| `vuls_package_parser` | future-architect/vuls | 混杂命令输出、解析边界 |
| `teleport_last_mfa_device` | gravitational/teleport | 认证状态与安全相关修复 |
| `openlibrary_missing_authors` | internetarchive/openlibrary | 缺失数据、转换边界 |
| `navidrome_audio_config` | navidrome/navidrome | 配置加载、模块迁移 |
| `proton_dropdown_refactor` | protonmail/webclients | 前端组件接口重构 |
| `qutebrowser_coordinate_parser` | qutebrowser/qutebrowser | 统一解析、非法输入 |
| `tutanota_decryption_cache` | tutao/tutanota | 缓存一致性、加载器间状态传递 |
| `openlibrary_http_refactor` | internetarchive/openlibrary | HTTP 客户端迁移、异常处理 |

每个仓库至少一题，OpenLibrary 的额外一题补充显式重构与异常处理。
规格长度约 2,050–6,278 字符，便于控制初始提示词体量；这不是实际 Token 消耗预测，
仓库检索、工具输出和多轮历史仍可能占主要消耗。

**运行协议**

- `deepseek-flash`，temperature 0，thinking 关闭，单 Agent、单工作区，顺序运行。
- 首轮每题一次；比较配置时使用全部相同题目和预算，交替运行顺序。
- 每题最多 24 步、48 次工具调用、600 秒和 75,000 累计 Token。
- Token 采用 provider 输入与输出用量；缓存输入是输入的子集。包含失败调用已报告用量、
  失败任务和预算耗尽任务；未知用量独立标记。
- 当前 Runtime 在响应返回后才知道实际用量，最后一个在途响应可能超出名义上限。
  运行时须记录超量，达到任务或整轮预算后不得启动下一次调用。
- 保存全部题目的结果和 Trace。模型宣称完成不计成功；预算耗尽也计入总任务数。
- Agent 只获得题目规格与初始仓库，网络只授权模型端点；判据与参考解不得进入其 Context。
- 捕获 Patch，在新的官方任务镜像中应用并运行原验证器，避免 Agent 对环境和测试的改动污染判据。
- 发布为“自选 12 题、Token 受限子集”，不得标作官方完整 SWE-bench Pro 分数。

主要指标为留出判据通过率、总/平均/P50 Token、包含失败消耗的每次成功 Token、
步骤、工具调用、耗时与失败类型。12 题首轮用于开发反馈，不能估计一般编码任务的稳定成功率。
这组题目中的安全相关代码修复不替代 Mini-dsh 自身的攻击性隔离测试。
调整题目或预算必须递增协议版本并重跑完整对照，不能因某题失败或耗尽预算而删题。

**执行状态与复现**

当前已完成选题、完整任务树核验和容器评测入口实现。正式成绩以结果目录内的
`report.json` 为准；软件测试不算公开任务模型运行。`selection.json` 是选题锁定文件，
由专用入口加载，不是本地 `mini-dsh benchmark` 的 fixture manifest。

容器入口按三个阶段执行：

```sh
uv run python -m minidsh.benchmark.public_swe prepare
uv run python -m minidsh.benchmark.public_swe check --image-mirror ghcr.1ms.run
uv run python -m minidsh.benchmark.public_swe run --credentials-file .env.deepseek
```

默认源文件目录为 `benchmark-results/public-swe/sources`，结果目录为
`benchmark-results/public-swe/baseline`；通过 `--sources`、`--output` 指定其他路径。
需要正在运行的 Docker（支持 Linux/amd64）；缺少引擎时在构造模型前退出。
不降级为宿主机直接执行，不自动接受 Docker 的首次启动协议。

`--image-mirror`（或 `MINIDSH_IMAGE_MIRROR`）可指定 GHCR 的国内 TLS 镜像入口。
这里采用[毫秒镜像的公开 GHCR 代理](https://1ms.run/guide)：先从上游解析 digest，
再按该 digest 下载镜像并核验架构和内容；已有官方缓存直接复用。
代理不可用时回退官方源，下载入口与 digest 均记录在结果目录中。
代理仅用于镜像下载，Agent 与验证器继续断网，DeepSeek 凭据不经过该代理。

Apple Silicon 上需确认 amd64 仿真能运行所选工具链；已观察到旧 Go 运行时在 QEMU
下发生 `lfstack.push` 崩溃，NodeBB 的 Redis 也发生段错误。需完成 Rosetta 安装并启用 Docker
的 Rosetta 加速后重新验证，或使用 x86_64 Linux 引擎；不能把环境故障计作模型失败。

`prepare` 仅从 GitHub 下载固定版本的所选任务，校验完整 Git tree（包括官方判据与参考解），
不安装或执行任务代码。`check` 下载固定镜像并锁定 digest，依次确认参考 Patch 通过、
空 Patch 失败，保留镜像清单与门禁日志。`run` 要求全部门禁与当前规格、镜像和桥接代码
指纹一致，完成后每题刷新一次包含完整 12 题的报告与原始 Trace。

Agent 在独立持久容器内使用固定的 `shell`（argv 数组）与 `git` 工具，
无宿主目录或 Docker socket 挂载。Core Loop 使用 Full History，20,000 字符工具输出上限，
关闭 local-first、progressive-tools、error recovery、工具重试和模型调用重试。
这两种工具的协议与本地五工具评测不同，不能直接把成功率差值归因于 Context 策略。
新对照应锁定同样的工具集合、模型和预算。

DeepSeek 密钥从宿主进程环境或指定凭据文件读取，不注入任务容器。
相对固定初始提交捕获 Patch，包含新文件，并在全新容器执行官方验证器。
预算耗尽时的部分 Patch 仍可验证；Loop 状态与验证结果分别保留。
任务 Token 上限与整轮剩余额度取最小值；用量未知的模型调用会停止后续任务。
未运行题保留在分母中，并标明原因，不冒充已执行失败。

已有 `report.json` 的目录拒绝重新运行，避免覆写和未计入的重复消耗。
暂不支持模型阶段断点恢复；强制终止时最后一个任务的 Token 需从 Trace 审计恢复，
不能直接自动重跑。比较第二个配置需使用新目录并重新执行门禁。

镜像目前记录官方 tag；正式运行前必须解析并保存不可变 digest。
然后在所选镜像上先验证参考 Patch 通过、空 Patch 失败，再调用模型。
当前 Agent 和验证器均断网。若某题验证阶段仍需下载依赖，参考 Patch 门禁会阻止模型运行；
需增加按主机控制的依赖准备服务或固定预置依赖镜像，重新核验门禁，
不通过通用 bridge 网络扩大权限。该实现约束见 [ADR 0015](adr/0015-public-container-benchmark.md)。
上游的 50 分钟协议被这里的 Token 和时间预算替换，因此只能在相同自定义协议内比较结果。

[task_ids.txt](../benchmarks/public_swe/v1/task_ids.txt) 从 `selection.json` 生成，
按清单顺序列出上游 ID。重新生成：

```sh
uv run python - <<'PY'
import json
from pathlib import Path

root = Path("benchmarks/public_swe/v1")
selection = json.loads((root / "selection.json").read_text())
body = "".join(task["upstream_id"] + "\n" for task in selection["tasks"])
(root / "task_ids.txt").write_text(body, encoding="utf-8")
PY
```

重新获取来源时检出固定提交，核对清单中三个文件的 SHA-256 和任务树 hash。
规格、判据和镜像来源见
[固定版本的上游说明](https://github.com/scaleapi/SWE-bench_Pro-os/blob/66f92766bba642462d4bbe5479e83f91f9211862/v2/README.md)。
