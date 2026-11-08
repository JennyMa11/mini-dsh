# Memory Review — v1

人工触发的候选审阅提示词。仅依据已脱敏事实和独立证据引用，判断是否存在适合当前项目长期保存的知识；没有合格项则输出空列表。不得保存原始 Prompt、完整工具输出、Secret、隐藏判据或单次任务进度。

每个候选输出：kind（environment / repair / diagnosis）、topic、content、适用 paths、error_type（无则 null）、来源 run_id、证据引用、适用版本、尚缺证据及与已有条目的冲突。正文不超过 2 KiB；未知信息显式标记，不生成虚假的来源/hash。

环境约束需要操作者确认的配置来源；修复和定位经验需要独立验收证据。将偶发现象与可复用结论分开，说明触发条件和失效边界；与已有项重复则提出人工修订建议，不直接覆盖。

输出只是待审建议，不代表 validated、approved 或 committed。若由操作者选择交给现有 propose_memory 工具，必须满足其真实 Schema；不能直接导入此报告或绕过 Tool Runtime、内容检查、独立证据验证和运行外人工批准。不得自动写数据库或声称已保存。
