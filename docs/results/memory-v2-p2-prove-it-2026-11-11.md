# Memory v2 P2 开发版本 prove-it

状态：**开发复核通过，P2 阶段未签署；P3–P5 未完成。** 本记录只描述下列冻结源码，不覆盖后续更改，也不回填旧 P0/P1 记录。

源码：`feat/memory-system`，基线 `a7c5d3bb0eff990e701051e6262a62a82bfbf349`，完整源码清单 hash `53b7ebff8756bd9a574581ef2429f8f93571032924e6230a46f023a5fc4a9d16`。清单包含 src/tests/examples、project/lock、AGENTS 和 P1 判据/设计/ADR；本报告及其他展示文档不在该身份内。改动尚未提交。

完整命令、退出码、耗时、输出、源码逐文件 hash、case/产物 hash 见 [机器记录](memory-v2-p2-prove-it-2026-11-11.json)。本机原始产物位于 `benchmark-results/extension-v1/memory-v2/p2-development/prove_it_1/`，包含实际 Trace、ModelRequest、副作用见证与容器回执；该目录被 Git 忽略，未宣称远端已有可下载证据包。

| 检查 | 实际结果 |
| --- | --- |
| `uv run ruff format --check .` | 302 个文件无需更改 |
| `uv run ruff check .` | 通过 |
| `uv run mypy src tests` | 170 个文件，零错误 |
| `uv run pytest -q` | **1090 通过，0 失败**，52.30 秒 |
| Memory discovery/progressive + 既有证据采集器 | 81 case 通过，55 次真实 Loop 记录；19 份请求见证、24 份数据库前后 hash 见证 |
| 原生 Docker 世界、超时、取消、错误恢复 | 4 个探针，32 个预声明布尔判据通过 |
| v1/v2 离线示例 | 均退出 0，真实模型调用 0 |

本轮新增的三条完整 Loop 检查覆盖：受信的第二 Store 实例 revise/reject，或来源文件实际 bytes 改变后，先前已在 ModelRequest 暴露的正文不再出现在后续实际请求；原始工具历史和 JSONL 保留。证据变化分支检查数据库 bytes 不变；修订/拒绝分支预期数据库变化，未把操作者写入错算为只读路径失败。

Docker 检查包含：已知宿主私有库的实际读取/写入失败、宿主库不变、绝对路径文件工具拒绝、合法工作区读写、只有工作区挂载、1 CPU/1 GiB、ARM64、禁网和真实 connect 失败、去除 capabilities/no-new-privileges，以及独立 session 子进程启动后整容器停止和删除。错误恢复探针实际执行失败和成功 Python 进程，核对错误解除后的请求撤回。镜像 digest 与各探针判据/hash 保存在原始 criterion/protocol；模拟模型仅控制动作，不替代文件、SQLite、证据或 OS 隔离。

所有本轮源码逐文件 hash、81 份 check 的身份、55 份 Trace hash、请求/数据库见证 hash 和四份 OS 判据 hash 已在归并前重新核对。`prove_it_0` 的 lint 失败记录保留；测试夹具拆分复合断言和去除冗余断言后重新冻结为 `prove_it_1`，没有覆盖旧回执。

## 未验证到的与后续关卡

这些通过数不能签署 [MV2_01–20](../memory-v2-acceptance.md) 的所有子输入。当前 case 使用分散的真实夹具；尚未逐项证明 P1 共同夹具的完整首请求 ID/条件集合、五候选中证据变化与 grant 撤销同时发生且不递补第六条、单独拒绝每一种读取权限、五条最大正文的完整包裹/投影上限、Store/来源各故障分支及全部内容字段的既定安全探针。默认关闭和 v1 回归通过现有测试，但还需在覆盖矩阵中明确关联实际节点与判据，不能用全量通过替代。

未签署项保持未测/部分覆盖。P3 结构化笔记与历史、P4 运行外整理均未实现；P5 未运行真实模型配对实验，不能宣称检索或整理提升成功率、节省 Token。fixture_operator 的合成批准不代表真实用户批准。后续继续当前 P2，达到阶段门禁后才进入 P3。

截图：不适用，本次为后端与命令行检查；使用实际请求、Trace 和副作用回执作为证据。
