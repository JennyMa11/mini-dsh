# 新家族夹具：反馈覆盖通过，模型可学习性尚未测

日期：2026-11-07，依据[ADR0042](../adr/0042-learning-fixtures.md)。本轮完成新夹具、
确定性容器检查及划分修订；没有模型采样、训练、GPU租用或云端上传。

## 当前交付和划分

新增独立`LearningCase`契约与16个任务：每个名义家族两种跨模块缺陷组合。
公开`MiniTask.files`只含源码、README和公开测试；reference、mutation、私有判据及
split/version绑定留在控制面。原`development_tasks()`及H1–H3工作区、评分和门槛不改。

离线审查发现初稿中的path_router与media_negotiation都使用匹配和排序，保守归为
同一`match_rank`算法组。首次审计之后、任何模型调用之前，将media移到train、
record_diff移到test；数据版本变为`learnability_dataset_v2`，case ID及公开/私有
判据版本保持不变，因为候选源码和测试输入逐字节相同。

| split | 名义家族 | 题数 | 用于分组的算法组 |
| --- | --- | ---: | --- |
| train | path_router、media_negotiation | 4 | match_rank |
| train | dependency_batches | 2 | dependency_batches |
| train | table_join | 2 | table_join |
| test | record_diff | 2 | record_diff |
| test | alias_resolution | 2 | alias_resolution |
| test | greedy_packing | 2 | greedy_packing |
| test | expression_pipeline | 2 | expression_pipeline |

因此是**8个名义家族、7个算法组**，train有3组、test有4组，不将两种匹配任务当作
独立train簇。固定每题一次G4会给match_rank一半训练槽，其余两组各四分之一；这是
当前小数据的偏斜，后续需明示，不能宣称家族均衡。未来readiness的反馈恢复至少覆盖
两个不同算法组，不以path_router/media两者充当两个独立组。

本表取代[初稿](interaction-learning-readiness.md)中的候选split表；原初稿和私有
protocol-draft-01作为历史保留。没有根据模型成功率、训练结果或test轨迹选择划分。
新模型队列仍未冻结或准入。

## 真实检查及分母

每题固定7个候选：reference、两个缺陷合并、常数错误解、两个单独原始缺陷、
两个额外公开规格语义错误；先枚举全部112个候选，再分别执行公开/私有判据。

| 候选类别 | 声明实例 | 公开结果 | 私有结果 | 去重候选程序数 |
| --- | ---: | ---: | ---: | ---: |
| reference | 16 | 16通过 | 16通过 | 8 |
| 合并原始缺陷 | 16 | 16拒绝 | 16拒绝 | 16 |
| 常数错误解 | 16 | 16拒绝 | 16拒绝 | 8 |
| 单个原始缺陷 | 32 | 32拒绝 | 32拒绝 | 24 |
| 额外语义错误 | 32 | 32拒绝 | 32拒绝 | 16 |

合计**224次真实容器检查、72种唯一完整候选程序**。没有策略/语法拒绝、超时或未执行；
错误候选均为可执行测试失败，不把策略拦截算成语义检出。语义探针32实例实际16种程序，
两变体共用参考与额外探针，不能当作32个独立语义问题。

独立验证使用既有固定arm64镜像、禁网、1CPU/256MB和8秒执行超时，逐个新容器运行。
原验证器仍写`minirepo_dev_v1`标签；新`criterion-binding.json`另存实际case/probe、
检查类别、`learnability_private_v1`或`learnability_public_v1`、checker payload hash及
原verdict hash。没有覆盖旧sidecar，也不把旧标签当成新数据的完整版本。

分组修订之后没有重复跑224次容器。`metadata-v2/manifest.json`逐项核对16份公开文件、
reference及两套checker hash，以及112个候选hash全部相同，然后绑定新split/group。
896份原verdict/输出/binding文件保持原hash；实际执行源码archive与新metadata源码
archive分开保存。该派生报告是旧检查证据的重新绑定，不是新版本模型或容器实测。

## 覆盖能说明什么

当前有限探针包含：路径段边界/同分规则、拓扑批次/环、join重复匹配/顺序、字段删除/
None与缺失、alias多跳/环/实体优先、装箱精确容量/first-fit、媒体q=0特定拒绝、
表达式优先级/左结合/完整消费。公开与私有使用不同具体输入。

这证明这些预设错误有反馈可用，不证明判据穷尽全部规格，也不证明模型会读测试、
根据反馈修正或产生组内奖励变化。多次模型采样、G4有效组、训练更新和独立Base/RL
收益均为**未测**。旧H3综合门槛继续保持失败。

当前源码每题31–74行、6–7个可改源码文件；去除docstring后的AST语句数28–67。
没有为接近旧任务157–167行而加入无用说明或包装。行数不能标定难度；与旧任务的
难度匹配、4B成功率和12步是否足够，必须由将来的固定readiness测量，不能视为已知。
path_router共9个公开文件，其余8个；八项批量工具也不保证一次读完全部文件。

## 家族关系审查和局限

去除位置及docstring的整程序AST比对，覆盖148个历史case实例，没有完整reference
直接相同，也没有当前train/test完整reference相同。该检查保留标识符与模块顺序，
不是通用重命名检测或语义查重。

本轮实现者另按已给出的公开规格与源码比较主算法：

| 新任务 | 已见相近原语/任务 | 当前区分及剩余风险 |
| --- | --- | --- |
| router / media | wildcard_match、common_prefix，以及彼此 | 路由按段及method；媒体先确定特定range再评分；两者仍同组放train |
| dependency | flood_component、finite_automaton | 拓扑批次及重复边，不是连通域计数或字符驱动DFA；共享图遍历原语 |
| table_join | group_totals、query_pairs | 重复匹配笛卡尔组合，不是累加或简单解析；共享索引/分组原语 |
| record_diff | overlay、settings、table_join | 比较两版字段及新增/删除，不是覆盖合并或join；共享dict索引及None边界 |
| alias | finite_automaton、flood_component | 单链解析与每query环检测，不是DFA接受或二维连通域；共享状态/visited结构 |
| packing | inventory | 稳定first-fit选箱，不是全订单原子预留；共享容量减法与输入所有权要求 |
| expression | integer_text、bracket_stack、horner_polynomial | 显式token流和递归优先级解析；共享数字扫描、括号及算术原语 |

这是本轮编码助手的静态审查，不是独立人工盲审。不能据名称/AST不同宣称语义完全
独立、无预训练污染或训练/测试绝无相关性；source_family分组目前只显式合并确定的
匹配排序近亲，其他共享原语如上披露。正式扩展应加入独立审阅及更多算法组。

## 接口、后续及资源

- `learning_contracts.py`：LearningCase、FamilyDefinition、Mutation；显式split/group和版本。
- `learning_record_sources.py`、`learning_sequence_sources.py`：固定规格、reference和输入。
- `learning_tasks.py`：生成MiniTask及112个纯探针，拒绝同算法组跨split。
- `learning_gates.py`：只提供离线容器审计，无ServiceClient、采样或训练入口。

契约检查先写后实现；初次夹具检查发现表达式源码字符串中反斜杠转义导致语法错误，
在首次Docker审计前修复为raw literal。该开发失败不属于模型任务结果。
源码和共享账本在实际224项执行期间hash不变；原审计归档保持不变，随后只修订分组
元数据。当前没有实现MiniTask训练桥接，不把新数据契约视为完整训练器。

账本保持**42.667/50元**，可用**7.333元**，无新增provider费用或pending。
80条readiness/训练/评测草案的保守分配仍19.702元；分组修订不改变条数或资源上界，
资金缺口12.369元仍存在。尚未要求提额，也不先用余额启动readiness。

下一阶段先补typed训练桥接：将真实Token、policy/run/seed、独立verdict和新版本
转换到既有TrainingBatch，拒绝重编码、合成Token、跨组混合与未知RPC重试；再验证
完整训练/结束保存/评测资金。模型学习及算法比较目标保持未完成。

本地证据根`benchmark-results/trainable_agent/learning-fixtures-v1/`：
`audit-01/`为实际224次执行，`reference-overlap-01.json`为有限AST检查，
`metadata-v2/`及`rebind_metadata_v2.py`为新分组/源码绑定，非重新实测。
审计重跑只写全新目录，不覆盖旧证据：

```bash
uv run --project experiments/trainable_agent --extra lab python -m minidsh_rl.learning_gates \
  --output benchmark-results/trainable_agent/learning-fixtures-v1/audit-new
```
