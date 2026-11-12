# ADR 0033：手动整理复用单 Agent Loop，只发布未验证提议

状态：Accepted with limitations，P4按C01–C08验收通过。P3已按N01–N12验收。

整理采用独立的运行外命令与run_id，复用现有Model port、AgentLoop、ToolRuntime及预算。另造一个直接调用模型/数据库的总结循环会绕过已有计账与权限，因此不采用。每次只有一个顺序执行的整理Agent，不启动后台任务或第二个Actor；与共享MemoryService的Actor/维护操作互斥。

操作者以内联参数明确授权已脱敏事实及至多五个当前committed修订引用；不提供任意Trace/sidecar文件读取入口。输入hash绑定操作者身份、事实来源run/内容hash及完整笔记来源指纹。输入加载在每次模型准备前经独立工具权限/预算重新核对；模型只获得提议工具，不能调用加载器或validate/approve/commit/reject。提议自动带固定来源，模型不能增加来源或批准字段。可提出已授权来源的替代意图，但旧知识保持不变。

所有新提议使用整理run作为source_run_id，变更集和Trace绑定输入hash；来源关系不继承验证/批准，后续仍需新证据和操作者明确批准。已授权知识可以原样用于新提议，原始事实全文不能伪装成规则；登记禁止材料始终优先。只检查已知指纹，不宣称语义脱敏。未知用量、超额、中断及故障遵循Loop既有停止规则；不自动重试写入，已完整发布的proposed保留，原始Actor Trace与committed状态不修改。取消发生在发布后时须按source_run_id/历史读回，不声称回滚。

P4不引入新数据库schema，关联依据是不可变输入hash、独立run和追加Trace；结果可按run审阅。回退为不执行consolidate，既有读写和批准链保持。P5另行冻结真实模型配对协议，不能用本阶段合成证据认领收益。

阶段证据见[P4报告](../results/memory-v2-p4-2026-11-12.md)，真实模型收益未测。显式离线script在provider选择前直接分流，避免环境profile改变执行来源。
