# 公开反馈覆盖审计：定位语义盲点并制作独立草稿

日期：2026-11-07，分支 `experiment`，依据[ADR0040](../adr/0040-feedback-coverage-audit.md)。
本轮只进行离线Docker检查；无模型采样、无训练、无账本变更。H1–H3结果和门槛不变。

## 为什么审计反馈

[H3](batch-limit-results.md)已经观察到inventory/windows两家族的真实反馈修复，但四项与
八项读取仍均4/16成功。一条windows候选通过公开测试，却遗漏公开规格中的“相同时间
最后输入胜出”，最终独立失败。因此继续改工具数量或增加回合缺乏充分依据。

审计将原始缺陷覆盖与部分修复的语义覆盖分开，避免由“原bug会失败、参考解会通过”
推定反馈已经足够。所有探针在执行前枚举，未根据结果删改分母。

## 固定探针和实际结果

原8题、4家族各2变体；每题6个候选：reference、原始bug、常数错误解、逐个恢复的
2个原始缺陷，以及1个公开规格语义探针。48个候选分别运行原公开/私有测试，共96次。
候选程序去重后35种；其中8个语义实例实际为4种唯一程序，不当作8个独立问题。

| 错误候选类别 | 声明实例 | 原公开反馈检出 | 原私有判据检出 | 新草稿公开反馈检出 |
| --- | ---: | ---: | ---: | ---: |
| 原始bug | 8 | 8/8 | 8/8 | 8/8 |
| 常数错误解 | 8 | 8/8 | 8/8 | 8/8 |
| 单个原始缺陷 | 16 | 16/16 | 16/16 | 16/16 |
| 规格语义缺陷 | 8（4种程序） | **0/8** | 8/8 | **8/8** |

8个reference在原公开/私有判据均通过，在新草稿中也均通过；没有候选策略/语法拒绝、
超时或未执行实例。两个版本各48候选/96检查完整完成，共192次真实容器检查。
这些是有目的的有限探针，不能将表中比例当作自然任务成功率或全面语义覆盖率。

四类语义探针对应公开合同；新公开例子的生成函数不读取hidden_checks或参考源码，
输入值另行构造，不复制隐藏测试。这仍是同一开发任务上的定向补充，不是独立盲测：

| 家族 | 故意保留的错误 | 新草稿补充的公开反馈 |
| --- | --- | --- |
| inventory | 跳过库存不足的拒绝分支 | 不足时整单拒绝、返回缺口且原库存不变 |
| windows | 最新事件只在时间严格更大时替换 | 同一id/时间、不同channel时最后输入胜出 |
| settings | 将显式None当作缺失 | None保留为值，缺失路径才返回MISSING |
| billing | 用四舍五入代替向上取整税额 | 正的不足半分税额仍向上取整到1分 |

原公开测试能捕获所有已知原始缺陷，但以上4种部分语义错误都可通过公开测试。
这为H3的“公开通过但独立失败”提供了系统性解释；不意味着私有判据或Harness存在错误。

## 实际实现及隔离

- `feedback_coverage.py`提供纯`FeedbackProbe`构造器、分母明确的指标汇总及顺序审计入口。
  记录声明/已检查数、公开/私有检出、盲点、reference门禁和覆盖门禁。
- `feedback_fixtures.py`显式生成`*_feedback_v2`新ID开发夹具；只修改新对象的
  `public_tests.py`，保留原source、README/specification、reference及hidden_checks。
  不注册到既有模型队列，也不改变默认`development_tasks()`。
- 新公开例子放在最终成功标记之前，使用可操作的`contract:`断言提示；验证过程中
  只把候选与当前检查器放入独立容器，隐藏检查从不进入Agent工作区。
- `--draft-feedback`仅是离线审计选项；没有对应模型实验、SFT或RL训练授权。
- 每次检查复用原可信验证器及固定镜像，1CPU/256MB、禁网、8秒执行超时，顺序执行。
  结果写新目录，保留candidate hash、stdout/stderr与独立sidecar，不覆盖旧结果。

私有sidecar的criterion_version沿用未变的minirepo_dev_v1；公开反馈版本由新case_id和
checker SHA区分，不能仅凭共同的私有判据版本把两套公开反馈混为同一实验条件。

该修复是针对已发现4类盲点的定向覆盖。草稿通过相同探针不等于覆盖所有规格，
也不证明模型会利用新反馈、修复率提升或RL可学习性已解决。旧H3门槛仍失败，不能
用本轮确定性组件结果补认模型准入。当前草稿仍为原4家族，不是新家族train/test。

## 训练信号和资金判断

从冻结H3队列只读补算：两种上限各有4/8题的两次采样结果不同，另4/8题两次都失败，
没有两次都成功的题。这是“两seed结果变化”代理，不是G4实际优势组，也不足以可靠
估计成功概率或有效更新率。不能混合四项/八项轨迹构造一个GRPO组。

新草稿没有模型轨迹，因此其成功率、非零Advantage比例、预算压力和Token成本均未测。
下一次训练必须固定新数据/策略/环境，并为完整Base/RL及后续算法对照预留资源；
不能把旧开发集成绩当作新草稿baseline或独立泛化证据。

当前保守账本仍 **46.135/50元**，含2元不确定性预留，可用 **3.865元**，无pending；
它不是最终实付。本轮无provider调用，不租GPU、不使用重置卡，账本前后hash一致。
即使仅考虑“4组×G4=16条训练采样＋8题×2seed×Base/RL=32条评测”，
按现有0.09元/run完整预留规则仅采样就需4.32元，已高于余额，尚未计训练/管理费用。
这不是预计实付下界，而是当前安全准入规则下的资源缺口，不能用实际平均更低来绕过。

因此保持付费入口关闭：先核验可审计的已结算费用、设计新家族的反馈覆盖与完整训练
协议，再决定资金准入；不再自动执行第四轮同开发集工具校准，也不提前比较GRPO/GSPO。

本轮尝试只读刷新账单：agent-browser未能连接运行中的Chrome调试接口，CUA浏览器访问
host不可用，原生窗口的账单标签操作因用户窗口状态变化被中止。未读取新账单记录、
未更改登录/支付/浏览器设置、未释放任何预留。旧账单观察不能代替最新闭合账期证明。

## 证据与复核

本地根目录 `benchmark-results/trainable_agent/feedback-coverage-v1/`：

- `audit-01/`：原判据96次检查；source归档、执行前probe-manifest与完整sidecar。
- `draft-audit-01/`：新ID草稿96次检查；原私有checker hash保持一致，公开checker单独版本。
- `comparison-01.json`：逐case/probe的配对核对、去重统计、预算及H3两seed代理。
- `quality-gates-01.json`：实际静态/类型/回归检查，不用测试数量替代功能覆盖。

收尾检查：主项目1072项、实验包193项通过；Ruff与strict mypy通过（175/131文件）。
这些工程结果不补认模型可学习性、训练收益或未测安全能力。

```bash
# 均无模型或训练调用；输出路径必须全新，历史目录禁止复用
uv run --project experiments/trainable_agent --extra lab python -m minidsh_rl.feedback_coverage \
  --output benchmark-results/trainable_agent/feedback-coverage-v1/original-new
uv run --project experiments/trainable_agent --extra lab python -m minidsh_rl.feedback_coverage \
  --draft-feedback --output benchmark-results/trainable_agent/feedback-coverage-v1/draft-new
```

原审计执行时的源码与后来增加草稿入口的源码分别归档；重新评估须绑定对应source、
fixture与checker hash，不把新代码运行冒充旧版本实测。Trace、原始模型评分与H3测试
分母保持冻结。本阶段没有重验完整Memory/Skills、恢复、Secret或网络攻击矩阵。
