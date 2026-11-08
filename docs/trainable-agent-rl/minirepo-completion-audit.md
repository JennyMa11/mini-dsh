# 小仓库阶段完成核验

日期：2026-11-07。核验对象是[ADR0036](../adr/0036-minirepo-interaction-experiment.md)及
[执行协议](minirepo-plan.md)规定的当前开发阶段，含单次输出上限复核。后续RL/Pilot/
新test为明确门禁后的条件阶段，不把它们写成已执行，也不以本次工程检查代替。

| 要求 | 当前证据 | 判定与结论边界 |
| --- | --- | --- |
| 在实验分支工作 | git status --short --branch显示experiment | 已核验；未提交或推送，共享工作区原有修改保留 |
| 8题、4家族、多文件真实依赖与两个缺陷 | minirepo_tasks.py、minirepo_contract_sources.py；gates-06/summary.json记录fixture hash | 已核验；仅开发集，不是完整项目修复基准 |
| 参考可过、原bug/错解/各缺陷可检出 | 56条Docker检查：8参考私有、8错误私有、8常数错解、16单缺陷mutation、8参考公开、8错误公开 | 当前声明范围通过；不保证独立判据完备 |
| 四类工具复用Loop、权限与隔离 | minirepo_tools.py、minirepo_rollout.py；test_minirepo_tools.py、test_minirepo_rollout.py | Docker契约与真实Loop接线通过；核心agent_loop.py与Git基线无diff |
| 一次提交语义与真实Token区分 | SubmissionModel、协议/轨迹测试、两批原始Token与Trace绑定审计 | 单次成功Patch后锁定；合成终止另标记，不伪装为采样动作 |
| 完整32条配对诊断及保留失败 | base-interaction-01/queue.json、summary.json、analysis-01.json | 32/32完成，16配对；不按终态删除失败 |
| 保留首批并单变量512→1024 | output-cap-amendment-01.json、output-cap-source-proof-01.json、cap-comparison-01.json | 第二批32/32；Prompt AST及初始Token配对一致，其他资源额度不变 |
| 冻结源码、环境、轨迹与独立判据 | 每批protocol/source.json/source.tar.gz、tokens/trace/candidate/verification；analysis两份输入hash | 64条绑定通过；原始数据未覆写，派生评分另存 |
| 成功、效率、消耗、时间分开报告 | minirepo-results.md/json、diagnostics两份 | 两模式每档均1/16；不宣称RL或多轮收益，完整失败消耗保留 |
| 不确定性与机制代理 | 家族bootstrap与首次Patch事后复评分 | 仅4开发家族；退化[0,0]不证明等效；两条多次修改成功均缺少中间公开失败测试证据 |
| ¥50共享预算及停止条件 | calibrated-budget.json、advance-decision-01.json与ledger hash | 48.155保守累计，1.845剩余，pending=0；完整后续评测预留2.88不足，不训练、不打开test |
| SwanLab记录 | cloud-readback-final.json，6条汇总run均FINISHED，项目PRIVATE | 上传汇总并回读；原始轨迹、Token、候选和判据留本地 |
| 离线失败分类与候选协议 | failure-review两份及脚本；tool-cold-start-plan.md/example.md | 64条逐条分类，1,065个输入绑定复核；分类不是因果解释；新Prompt仅设计未运行 |
| 工程质量 | quality-gates-01.json保存7个命令/退出码/输出 | Ruff通过；root pytest1072、实验pytest154；strict mypy175/119；diff检查通过 |
| 可再生成交付与审计清单 | minirepo_delivery.py、minirepo-results.json、delivery-manifest-01.json | 输入hash检查后生成；清单排除自身，重建要求新输出路径 |

证据根目录：`benchmark-results/trainable_agent/minirepo-v1/`；财务账本另位于
`benchmark-results/trainable_agent/expansion-v2/calibrated-budget.json`。

## 当前停止是协议分支，不是收益结论

原协议明确“若无法容纳训练及完整评测就停止，不自动扩额”；单次输出修订也明确
“完成后只分析数据，不自动启动第三批或RL”。当前机制和资源两个准入均失败，
因此本开发阶段已经走到其预声明终点。新任务RL、SFT、工具成本Reward、Train24/
Test16及正式泛化实验均未实施，不标为完成或由旧任务训练结果替代。

用户后续要求继续规划时，新增的工具示例协议仍沿用累计¥50和完整队列预留，
状态not_admitted。下一项条件是有可审计账期和归属的服务商账单，或明确预算调整；
旧3.37入账观察、优惠或重置卡都不能自动清零历史消费。最终实际账单目前未知，
报告始终使用保守估算，不把未知填写为零。

Memory/Skills/恢复、Secret与网络完整攻击矩阵没有在本阶段重验；真实工具选择的
语义正确率和重复读取必要性没有逐条审核，保持未测。工程测试通过不能补认这些能力。
