# 开发反馈v3：冻结协议与执行记录

2026-11-08，用户明确要求针对混合奖励和反馈修复覆盖不足修订数据，冻结后重新
开发准入。范围见[ADR0051](../adr/0051-development-feedback-v3.md)。本轮仅24条开发，
无训练授权扩展；v2的24/216与失败门槛保持原样。

## 冻结修订

仅选原6道dev，不读取train/validation结果或用其难度筛题。curve_development_v3
继承相同spec、业务源码/缺陷组合、reference和curve_private_v2私有判据，只替换
public_tests.py为按合同逐条执行、统一汇报期望/实际/异常与输入所有权变化。
公开输入来自公开spec，生成函数不读取reference或hidden_checks，不复制/manual
私有尾斜杠输入。公开版本curve_public_feedback_v3，新case ID curve_dev_v3_*。

修订旨在让反馈区分路由的method-case/longest-prefix等原因，区分join的mode、
left-order、unmatched-value等合同，保留原错误而非挑易题。输入与格式一起改变，
属于组合反馈profile；无同期旧版本控制臂，不能宣称格式自身或RL的因果收益。
旧开发题已打开；本轮不是新的泛化测试集。

6题×7候选(reference/broken/constant/2单缺陷/2语义错误)×两套判据=84项容器检查，
所有候选和期望在执行前冻结。先reference全部通过、错误解由功能测试检出，且源码
和财务账本执行期间不变，再准入provider；不依靠静态拒绝替代语义检出。

## 固定模型队列与门槛

复用v2实际审计过的初始零LoRA sampler；旧archive hash和零B/结构证明重新本地核验，
实时只读检查owned run/checkpoint/URI/READY归属后才连接。无新trainer、optimizer、
export或checkpoint。初始权重与tokenizer保持相同。

Qwen3.5-4B/r16、tool_example、Full History、batch_read_eight、temperature1/top_p1。
每条12步/12工具、36k Token、3072总生成、6144单次总长、1024单次生成、180秒；
独立禁网arm64容器、1CPU/1GB；独立验证1CPU/256MB/8秒。原预算/安全边界不变。

固定6题×G4=24条，seed=926000+题序×1000+repeat×20，沿用v2配对seed；
不据中途结果换题、补采样、改Prompt或改阈值。保持原准入：至少3/6混合组且覆盖
至少2算法组，成功6–18/24，真实“公开失败→后续采样→新Patch→私有通过”覆盖
至少2算法组。partial私有检查不反馈给模型，所有失败和未知保留。

当前唯一财务账本仍learning-curve-v2/run-01/finance/budget.json，本轮不建新epoch。
在原累计150/新增100档之内叠加本轮3元硬上限，完整开发24×.09+.06=2.22元；
开始前必须整轮容纳且无pending。开始baseline写入冻结plan，重启不得重放未知调用。

## 证据与交付

拟保存review-v2、84检查、冻结plan/config/source/输入hash、原权重来源/实际采样连接、
逐条Token/logprob/Trace/候选/verdict、财务operation/UTC、反馈partial proofs、所有
准入条件的分子分母。公开汇总与私有SwanLab只含数值，不上传原始数据或私有判据。

当前协议为执行前记录，结果后追加；通过或失败均在24条结束后停止，不自动训练。

## 实测结果：开发准入通过，未进行训练

真实进程正常exit0，启动至24条结束789.02秒，含预检/已有sampler连接、容器准备和
反馈复核，不是GPU训练耗时。24/24完成，252次真实采样全部有start/return及财务绑定，
输入+输出699,368 Token，未知provider调用0，财务pending0。训练客户端、更新和
新checkpoint均为0；本轮已停止，不自动执行旧216条学习曲线队列。

| 算法组 | 独立成功/声明数 | 混合G4/组数 | 有独立证据的反馈后修复 |
| --- | ---: | ---: | ---: |
| match_rank / path_router | 0/8 | 0/2 | 0 |
| dependency_batches | 3/8 | 2/2 | 2 |
| table_join | 5/8 | 2/2 | 1 |
| 合计 | **8/24** | **4/6，覆盖2算法组** | **3，覆盖2算法组** |

冻结顺序的Reward为：

- dev_00：`[0,0,0,0]`
- dev_01：`[0,0,0,0]`
- dev_02：`[1,0,1,0]`
- dev_03：`[0,0,1,0]`
- dev_04：`[0,1,1,1]`
- dev_05：`[1,0,1,0]`

原准入条件全部满足：24/24完整、至少3混合组（实际4）、混合组至少2算法组（实际2）、
成功6–18/24（实际8）、真实反馈后修复至少2算法组（实际2）。阈值没有调整，也未补
采样或筛掉失败。通过只说明当前开发材料值得进入下一阶段，不证明训练收益或稳定泛化。

### 反馈证据与判据覆盖

3条完整路径为dev3_02_0、dev3_02_2（dependency_batches_04）和dev3_05_2
（table_join_05）。每条都有修改后公开失败、后续实际采样、新Patch、最终私有通过，
且修改前的对应partial snapshot经独立私有功能测试确认失败；私有检查不反馈给模型。
其余5次成功不冒充反馈恢复。所有24条机会检查齐全，8条出现修改后公开失败，恢复
为3/24全部声明run、3/8已触发机会；这些是描述性路径统计，不是反馈因果效应。

全部24条有独立判据结论；实际功能测试执行23条，dev3_01_1在静态规则阶段拒绝，
仍计入24条完整分母。coverage=1表示独立判据覆盖，不代表24条都运行了功能子进程。
原始verdict保留minirepo_dev_v1标签，typed binding及verification-bindings-01.json
明确run_id/case/private版本curve_private_v2/公开curve_public_feedback_v3/检查器hash。

### 与v2的历史开发参照

| 指标 | 历史v2 | 本轮v3 |
| --- | ---: | ---: |
| 独立成功 | 7/24 | 8/24 |
| 混合组 | 2/6 | 4/6 |
| 混合组覆盖算法组 | 1 | 2 |
| 反馈修复覆盖算法组 | 1 | 2 |
| 有证据的反馈恢复路径 | 3 | 3 |
| 推理Token，失败保留 | 669,604 | 699,368 |

同题/repeat沿用seed、同一初始sampler权重URI；历史v2独有成功2对、v3独有成功3对。
公开输入和诊断格式一起修改，且没有同期控制臂，只有6道已打开开发题、3算法组和
一次新采样队列，因此不能把净增1次成功或混合组变化解释为稳定/因果收益。路由仍
0/8，总Token反而增加；未证明工具效率或RL效果改善，未评估新家族泛化。

## 数据、费用、安全与复核

84/84判据检查完成：reference6在公开/私有均通过，36个错误实例均由两套功能测试
检出，无静态拒绝或超时替代；42个case/probe实例实际23种唯一程序，不能视为42个
独立问题。只验收本轮6道dev，不追认未运行的train/validation。

原初始archive实际重新本地验证结构和零B，原hash/URI/owned checkpoint实时核对，
仅创建采样连接，不重新导出或下载权重，不创建trainer。本轮额外保守占用1.515元
（采样1.455、元数据/连接.060），累计52.104元；3元本轮上限和既有累计150/新增100
档均满足，无pending。数字不是提供商精确实付账单；未充值或新建财务epoch。
唯一活动账本仍learning-curve-v2/run-01/finance/budget.json，旧费用/ID/预留保留。

运行前后真实安全探针通过：禁网、无宿主挂载、cap drop ALL、canary隔离且不变、
超时/取消后子容器移除。实际配置凭据文本扫描分别33,884/35,193份受检文件，命中0；
只支持这些检测对象/探针范围，不宣称所有Secret或全攻击面已验收。

本地`benchmark-results/trainable_agent/development-feedback-v3/`保存：

- review_previous.py/review-v2-01.json：只读复核原24条；8条未Patch且候选未变，
  首个错误提示重复。标签在查看结果后制定，不是预注册根因分类。
- audit-01：84份原verdict、source archive、case/probe清单和判据绑定；
  [公开覆盖摘要](../results/development-feedback-v3-coverage-2026-11-08.json)。
- run-01：冻结plan/source/config/来源权重证据、owned metadata/实际sampling连接、
  24条sealed轨迹/Token/logprob/Trace/候选、独立判据/财务operation/UTC与partial proofs。
- analysis-01.json和analysis-recheck-01.json：逐字段相同的离线派生，非重复模型实验；
  [公开完整指标](../results/development-feedback-v3-2026-11-08.json)。
- historical-reference-01.json、verification-bindings-01.json、feedback-opportunities-01.json：
  历史同seed配对、明确run_id判据绑定与完整反馈机会分母，不改旧分数或原verdict。
- execution-01-started/ended.json：真实进程起止、退出码、脱敏日志和控制脚本hash。
- security-before/after：真实副作用安全检查；整个运行期间执行源码hash保持不变。

私有SwanLab只上传数值与封闭配置并API回读匹配：
[开发准入记录](https://swanlab.cn/@JennyMa11/mini-dsh-agent-rl/runs/2tzqvt5c)。
未上传Prompt、原始Token、私有判据、代码或checkpoint URI；云端FINISHED仅表示
本轮开发指标写入完成，不表示已训练。full policy entropy仍未测，sampled surprisal
仅为采样代理；通用Trace中的请求前预算拒绝与真实provider未知调用分开。

离线复核使用新输出路径，不发起模型或训练调用：

```sh
uv run --project experiments/trainable_agent python -m minidsh_rl.development_analysis \
  --directory benchmark-results/trainable_agent/development-feedback-v3/run-01 \
  --output benchmark-results/trainable_agent/development-feedback-v3/analysis-new.json
```

## 阶段验收与后续边界

| 能力 | 检查方式/证据 | 状态 |
| --- | --- | --- |
| 仅修改公开反馈、原业务/私有保持 | test_development_cases及case/checker hash | 通过；旧v2仍失败 |
| 公开/私有能识别预设缺陷 | 完整84原verdict | 有限探针通过 |
| 实际反馈路径 | Trace、partial私有检查、最终verdict | 两算法组3条通过 |
| 混合G4与覆盖 | 固定24条完整rewards、state | 原阈值通过 |
| 全队列资金、既有账本不重置 | 冻结baseline、native账本/financial-calls | 通过；新费用完整保留 |
| 未知响应不重放 | test_development_execution，真实start/return | 契约通过；本轮无真实未知调用 |
| 新训练/学习曲线/算法比较 | 无 | 未运行/未测 |
| 新家族/稳定收益/提示因果 | 无同期对照或多seed | 未测 |

本Goal以开发准入与证据交付结束。下一阶段需独立冻结与新反馈profile一致的训练/
验证数据及协议，完成对应数据验收后再研究学习曲线；不得把本轮6道已打开开发题
当作未见验证集，也不直接将旧v2的dev_failed改成通过或启动旧训练队列。

## 完成检查

root Ruff format/check、175文件strict mypy、pytest1072通过；实验包179文件strict
mypy与pytest310通过。首次实验mypy出现内部错误，使用实验包独立缓存重跑通过，
没有升级依赖。最终原始Token/财务/版本/判据hash绑定完整，分析逐字段重算一致，
未知provider调用和pending均为0。无残留本实验容器。
