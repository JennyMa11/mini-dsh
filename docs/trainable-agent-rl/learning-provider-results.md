# 新任务SDK适配：离线契约、持久策略与反馈证据

日期：2026-11-07；分支`experiment`；依据[ADR0045](../adr/0045-learning-provider-adapter.md)。
本阶段新增具体RuntimePort实现，**仅做假SDK客户端和固定脚本容器验收**。
没有创建真实ServiceClient、调用远端采样/训练或上传新SwanLab数据。
目标“交互学习收益，再比较RL算法”仍未完成，本文不提供新的模型成功率。

## 1. 接口依据与实际代码

核对已锁定PyTRIO 0.2.9本地源码的9个方法签名、3个SDK源文件hash，记录为本地
`learning-provider-v1/sdk-interface-01.json`；只import及inspect，不创建客户端。
官方[保存说明](https://docs.pytrio.com/docs/guide/save)区分Sampler与含optimizer的
Train checkpoint；[恢复说明](https://docs.pytrio.com/docs/guide/resume)要求使用
`create_training_client_from_state_with_optimizer`恢复训练。未采用临时匿名sampler
或仅恢复权重的路径冒充完整恢复。公开文档最初尝试的旧URL不可访问，依据为以上实际页面。

| 新增代码 | 输入 | 实际职责 |
| --- | --- | --- |
| `learning_sdk.LearningSDKRuntime` | 显式service、已持锁CostStore、root、ProcessRunner、tokenizer pin | 实现initialize/restore/rollout/feedback/update，不自行创建service |
| `learning_sdk.LeasedResources` | caller持有的CostStore | 提供available/committed/pending；关闭或失效的账本不能提供资源能力 |
| `learning_sdk.SavedPair` | 两类保存回执、training/sampling ID、tokenizer摘要 | 绑定持久策略、验证checkpoint角色和模型，不保存SDK对象或凭据 |
| `learning_feedback.verify_feedback` | 封存rollout及其目录、独立输出目录、runner/image | 检查反馈路径并复评修改前候选，只在控制面保存证据 |

rollout继续调用`run_learning_rollout`，使用原Prompt、八项读取、固定预算与原Loop。
采样继续用PricedBackend，更新继续用TrainerV2的原生PPO路径、sample_mean补偿和
fresh-policy guard；没有新写GRPO Loss或改变Reward。SDK初始化显式使用4B、rank16、
seed17、train_mlp/attn=true、train_unembed=false。真实导出的LoRA设置仍须新链路数值
准备核验，不能仅凭构造参数承认与旧tensor完全相同。

## 2. 策略身份、保存与恢复

初始化先预留create-client额度，再创建LoRA client；模型ID及tokenizer pin不匹配，
即停止后续保存与采样。tokenizer摘要包含词表hash、template hash、固定renderer和
thinking=false；每次rollout前再检查，防止内存中template变化后仍沿用旧身份。

初始及每次有效更新均保存Train和Sampler。Sampler保存与其客户端创建是一个受预留
保护的组合操作；两者都确认后才发布SavedPair。策略ID由该记录摘要派生，PolicyBinding
的snapshot_sha256是**保存回执元数据摘要，非远端权重tensor文件校验和**。
它只绑定可信控制器取得的服务回执，不认证任意外来文件或任意注入service的真实性。

有效组顺序为：backward → 数值guard → optimizer → Train保存 → Sampler保存/连接 →
journal checkpointed → UpdateReceipt。optimizer确认后保存/连接失败，没有回执，
外层控制器仍拒绝重放。零信号保留原checkpoint并发布skipped，不额外保存或调用SDK。

恢复读取控制器持久化的resume状态，确认原pair，再从optimizer URI新建training client，
重验tokenizer，分别重连原initial Base及current RL sampler。不会导出新权重替换Base，
也不新建零LoRA冒充续训。最多两次恢复及未知结果处理沿用ADR0044；实际SDK/Adam恢复
数值仍未测。路径和policy ID不符合本控制目录约束时，在SDK调用前拒绝。

## 3. 反馈恢复的判据

先重新读取原封存rollout，核对operations与Trace中每一次成功工具执行。只有下列路径
才成为待复评机会：有修改的公开测试非零退出 → 后续确有一次模型采样响应 → 新Patch。
同一模型响应中的连续动作不算“看反馈后重试”。该口径是可观察顺序代理，不证明
模型语义上利用了反馈，也不能把最终成功唯一归因于该条信息。

最终候选必须已独立通过；然后在新的隔离容器中复评公开失败时的候选快照。
只有实际执行、未超时、正常非零退出的私有检查失败，才计入pre-revision failure。
policy/语法拒绝、超时及exit0却缺验证标记，不算这种语义失败。私有checker及复评结果
始终不进入Agent工作区、Prompt或后续训练轨迹；Reward仍取最终独立0/1结果。

每份FeedbackProof保存输入hash、检查索引、返回事实和本地验证artifact摘要。
完整回执可在readiness尚未整体确认的重启后复用，不重复容器检查；输入或artifact
变化则拒绝。本阶段回归实际复现并修复了“完整反馈目录存在导致恢复中断”的问题。
目录存在但回执不完整时保持停止、需离线审计，不自动覆盖；不声称所有本地崩溃位置
都已经自动恢复。整体readiness确认后的缓存规则仍由外层控制器负责。

## 4. 已验收与未验收

定向19项通过；本地JUnit、测试输出及保留目录在
`benchmark-results/trainable_agent/learning-provider-v1/contract-suite-01*`。

| 检查范围 | 证据和禁止行为 |
| --- | --- |
| 初始化/恢复 | 假service精确调用顺序；初始0.21、恢复0.01仅为临时假账本额度；无真实收费 |
| 有效更新 | 真实TrainerV2代码配假forward/optimizer返回；两类保存完成后才有回执 |
| 未知操作 | optimizer超时或optimizer后sampler连接超时；无提交回执、不重复调用 |
| 无信号 | 无SDK额外调用/预留，原checkpoint不变 |
| 身份与接线 | tokenizer改变、policy元数据改变拒绝；PricedBackend/原case/seed/policy透传 |
| Base/RL隔离 | 恢复时连接不同保存URI，不新建Base、不导出替代权重 |
| 反馈代理 | 缺后续采样、operations/Trace不一致不认恢复；超时/拒绝不当语义失败 |
| 本地证据复用 | 已完成检查第二次无checker调用；篡改私有检查artifact后拒绝 |
| 真实容器脚本 | table_join固定脚本interactive有失败后修复、single无该路径；来源均scripted_control |

假SDK记录中出现provider型PolicyBinding/trio:// URI是接口测试输入；整个suite是
contract_fixture，不能把它们导入科学样本。容器脚本使用合成Token/logprob，不是4B采样。
测试目录中的篡改和未知操作用例保留负例，不能把其最终目录当作可恢复的研究运行。

未验收项：真实服务身份/权限、远端权重及optimizer数值、完整provider执行入口、
新任务readiness、学习收益、正式指标汇总和算法对照；完整安全攻击矩阵也未重验。
当前没有真实provider执行CLI，learning_prepare仍只读准备，不因适配类存在就准入。

完整工程回归：主项目1072项（52.19秒）、实验包243项（20.62秒）通过；Ruff格式/
静态检查通过，strict mypy分别覆盖175/152文件。命令、退出码、原始输出与JUnit保存
为本地quality-gates-01，测试数不替代上述未测项。只读财务复核实际持有CalibratedBudget
租约并使用LeasedResources，仍在factory前拒绝，调用数0、账本hash不变。

## 5. 预算及下一步

活动账本仍保守占用42.667/50元，可用7.333元、pending=0，hash保持不变。
完整80条方案仍分配20.302元，缺口12.969元，不提额、不先消费部分readiness。
Sampler保存/连接共享原每次0.10管理额度，恢复及Base/current重连共享0.01额度；
它们是规划预留，未验证实际管理收费或复杂失败后能否覆盖所有准备。

后续先形成有源码/config/tokenizer/报价绑定的真实执行入口和数值Smoke准入方案，
完善最终指标派生；真实数值调用及完整研究队列仍受预算和用户实验指令约束。
旧orchestration-v1/prepared-01冻结198个源文件，新阶段增加SDK/feedback源码后会被
当前加载器拒绝，属预期；保留旧archive，用新目录准备，不改写旧hash或历史结果。

本地`learning-provider-v1/prepared-01`为当前200文件源码的完整80条计划；
`admission-01.json`及`factory-block-01.json`保存财务拒绝，`sdk-interface-01.json`
保存静态接口核对。只读复核可执行以下命令，不创建SDK客户端：

```bash
uv run --project experiments/trainable_agent --extra lab python -m minidsh_rl.learning_prepare \
  --output benchmark-results/trainable_agent/learning-provider-v1/prepared-01 --admission-only
```

源码变化后须用对应archive分析或新目录准备，不能覆盖当前证据。

`audit_evidence.py`是本地纯标准库证据检查器，依据plan/archive、JUnit、命令回执及
准入报告重建新manifest，不访问SDK或改写账本。使用全新输出名，例如：

```bash
python3 benchmark-results/trainable_agent/learning-provider-v1/audit_evidence.py \
  --output benchmark-results/trainable_agent/learning-provider-v1/review-02.json
```

其检查只覆盖指定本地证据，不替代模型效果或提供商账单审计；锁文件及pytest目录别名
不作为不可变事实输入。contract-test-source.tar.gz另存本次三个检查文件，便于归档复核。
