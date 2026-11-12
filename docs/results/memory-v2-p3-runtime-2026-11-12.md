# P3 存储、运行时与 CLI 开发复核

状态：**核心链路已实现，P3阶段未验收。P4/P5未开始。** [P2封存](memory-v2-p2-2026-11-11.md)保持原样；本记录是后续源码，不能把旧OS成绩套到新迁移/备份。

已交付：显式SQLite version1→2迁移与hash备份、保留原payload的迁移快照、追加历史和分页、历史committed引用查询、人工结构化合并/修订、旧证据拒绝、明确批准后的原子替代、普通工具绕过拒绝，以及不创建模型的 `mini-dsh memory` 操作者入口。配置/命令见[使用说明](../memory-v2-usage.md)，当前范围见[执行记录](../memory-v2-execution.md)。

| 检查 | 当前结果 |
| --- | --- |
| 四项工程门禁 | 全通过；format 317文件，mypy 179文件 |
| 全仓pytest | **1202通过、0失败**，56.57秒 |
| P3四个feature测试文件 | **84通过、0失败**，覆盖domain、真实SQLite、Tool Runtime、CLI和组装 |
| local/configured × 七Context策略 | Actor提议→操作者验证/批准/提交→新run读取→Conversation恢复通过；42次Loop记录 |
| 真实存储资源/故障 | 历史增长到8MiB附近后拒绝新写入，无淘汰/部分发布；迁移取消/发布失败、备份冲突/符号链接/硬链接、原子替代过期/失败均有检查 |
| 变异回归 | 私有源码副本移除结构化写入的显式历史门禁后，原2项通过变为2项失败；生产源码未修改 |

source manifest、完整命令/退出码/输出、84份check的hash、请求/副作用/操作者Trace见证、变异日志与源码归档hash见[机器记录](memory-v2-p3-runtime-2026-11-12.checks.json)。原始产物在本机 `benchmark-results/extension-v1/memory-v2/p3-development/storage_runtime_0/`，被Git忽略，未声称远端发布。归并时复核当前源码、criterion、check、Loop Trace及全部见证hash，源码归档逐文件核对。

新增CLI明确执行一个操作者工具，不构造模型、不隐式批准提议，deny_tools仍优先。测试中的fixture_operator和配置确认均为合成临时资料，不代表真实用户批准。来源关系保存全部引用；新主张仍需专属证据，不能把多条旧来源当成独立验收。

开发红灯和夹具错误保留在[执行记录](../memory-v2-execution.md)：包括缺少API/模块、错误的异步all、缺少导入、重复参数装饰器及类型/lint错误。曾把Actor自己写出的提议文本出现在事实历史误判为注入；现在检查proposed/无批准和知识投影，保留实际历史，未修改生产边界来迎合错误断言。

未验证到的：N01–N12逐行最终签署、发布后通知失败/读回等剩余分支、完整P3离线例子，以及本源码上迁移后数据库/备份的OS隔离和中断探针。84通过不能代替这些证据。没有真实模型调用，不报告机制收益；P4/P5仍待执行。截图不适用。
