# P3 发布边界修复复验

状态：**P3仍在开发，未阶段验收。** 之前的[1202/84开发复核](memory-v2-p3-runtime-2026-11-12.md)只覆盖当时快照，不覆盖本轮新增的取消与损坏库分支；原记录保留。

进一步细化N06先复现了两项缺口：在before_storage_publish收到取消后仍发布迁移；version2缺失历史表却返回“已迁移”。底层发布函数现必须接收取消Event，五个调用点由mypy检查，并在原子文件替换前统一复查；读取/重复迁移检查必需历史列，损坏时拒绝、不重建。

发布检查点之后的取消或通知失败不能当作回滚。新增真实SQLite检查确认库已迁移、原payload保留、历史可读，重复迁移不写入；发布检查点前取消则等待线程收束并保持原库。新回归均先观察红灯再修复。

| 验证 | 结果 |
| --- | --- |
| `uv run ruff format --check .` / `ruff check .` | 318文件无需格式修改；lint通过 |
| `uv run mypy src tests` | 179文件，零错误 |
| `uv run pytest -q` | **1206通过、0失败**，56.63秒 |
| P3四个feature测试文件 | **88通过、0失败**；42次Loop、14份实际请求、15份副作用、33份操作者Trace见证 |

完整源码身份、逐文件hash、门禁输出、88份check及所有见证hash见[机器记录](memory-v2-p3-publication-2026-11-12.checks.json)。本机原始产物和逐文件核对的源码包在 `benchmark-results/extension-v1/memory-v2/p3-development/storage_runtime_1/`；本轮使用新目录，不覆盖storage_runtime_0，被Git忽略且未宣称远端发布。

已实现的迁移/历史/人工合并和原子替代、结构化提议、两条组装×七Context策略、Conversation和无模型CLI仍通过回归。fixture_operator只用于合成临时资料，未批准真实记忆种子；真实模型调用0。

未验证到的：P3完整离线示例、N01–N12最终覆盖签署，以及本冻结源码上迁移库/备份的OS隔离与中断探针。P4/P5未开始，不能用当前通过数代替这些工作或报告机制收益。截图不适用。
