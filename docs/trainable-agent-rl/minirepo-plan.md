# 小仓库多轮修复：执行协议

日期：2026-11-07；依据用户“按照上述规划继续”的目标及 [ADR0036](../adr/0036-minirepo-interaction-experiment.md)。
当前阶段先完成任务与交互诊断，后续训练须同时通过数据、机制和费用门禁。

## 范围与任务

独立实验包复用原 Loop、Tool Runtime、Permission、Trace 与 Docker；不增加核心
Memory、多Agent、网络或训练依赖。新增 `minirepo_*.py` 实验适配器。

开发集4家族×2变体=8题：库存预留、事件窗口、配置合并、账单计算。每题6个
源码文件、157–167行源码，另含公开测试和README。每变体有两个跨模块缺陷。
输入校验和公用API属于真实调用链，不能为通过测试删除。允许纯Python与受控
项目顶层函数 import；参考代码、私有断言和判定标签只存控制面。

初始合成fixture采用内容hash固定（不是外部Git仓库checkout），同时记录项目源码
快照、镜像digest和tokenizer模板。当前8题全部属于开发，不宣称新held-out test。
未来候选为Train24/Dev8/Test16，按12/4/8个缺陷家族拆分；尚未生成的训练/测试题
明确保持未实施，不把开发变体分入test。

## 工具与交互变量

仅向模型暴露read_file、search_repo、apply_patch、run_tests。所有执行在独占、无网络、
1CPU/1GB的arm64 Docker容器，模型不可任意执行Shell。Patch的全部旧串先校验，
逐文件原子替换；预检失败不写入，I/O失败尝试回滚，跨文件灾难恢复未验收。
公开测试及README不可改；私有验证在另一个新容器。路径遍历、外部/嵌套导入、
I/O和反射候选被拒绝；OS隔离继续是安全边界。

两组共享同一个已验证零LoRA的初始策略checkpoint、原生renderer和采样参数：

| 变量 | 一次提交single | 多轮修复interactive |
| --- | --- | --- |
| 读、搜、公开测试 | 提交前可用 | 全程可用 |
| Patch | 允许一次成功的多文件提交；无效Patch不算成功提交 | 预算内可多次提交 |
| 提交后 | 候选立即锁定，控制器产生明确标记的零采样Token终止响应 | 可根据测试结果继续修改 |
| 资源 | 同为12步/12工具、36k总Token、3072输出、每次512、6k上下文、180秒 | 相同 |

这比较的是反馈利用与再次修改机会的整体流程，不能解释为RL收益或单独的
测试反馈因果效果。Prompt仅说明任务、能力与约束，不规定读取、编辑、测试顺序。
控制器终止响应不进入采样Token/Logprob，不可伪装为模型动作用于训练。

## 固定队列与费用

8题×2 seed×2模式=32条，16配对；seed=54017+题号×10+repeat。
配对内case和seed相同，执行顺序交替，顺序单Agent执行；失败/取消/预算耗尽和
未运行case保留预声明分母。未知provider调用留下pending，停止而不重放。

当前累计保守估算44.199元、可用5.801元。诊断阶段上限3元，完整队列预留2.88元。
无缓存输入/输出费率沿用本轮SDK只读核对的每百万1.49/4.54元。
单条最坏连续费用加逐类别舍入余量：

`(36000*1.49 + 3072*(4.54-1.49))/1e6 + 12*2*.001 = .0870096 < .09元`。

每条预留.09元；实际按逐RPC真实Token保守结算，旧预留与失败不清零。
按最多全部32条上界，累计最多47.079元，仍保留原2元不确定余量。不是实际账单。
SwanLab只上传白名单汇总/配置，原始轨迹、参考和判据留本地；不启用付费套餐。

## 验收与阶段门禁

付费前56项Docker任务检查：8参考私有pass、8错误私有fail、8常数错解fail、16单独
缺陷mutation fail、8参考公开pass、8错误公开fail。另以确定性Model跑完整Loop，
检查工具权限链、路径保护、Patch预检无部分修改、提交限制、真实Token记录与
合成终止标记。版本/判据/源码hash在采样前冻结。

分析主要指标为独立修复成功率、配对差值和按4家族bootstrap 10000次的探索区间；
种子20261106、nearest-rank分位数。工具次数、所有失败在内Token/成功、端到端
延迟与终态分别报告。4家族区间不代表大规模泛化或训练seed不确定性。

机制证据：多轮成功轨迹若有多个Patch，运行结束后在独立新容器验证第一份
Patch，再结合公开失败测试发生于第一次Patch与下一次修改之间的记录，标记
“初次候选失败→公开失败反馈→再次修改→最终通过”的代理案例；不把它自动称为
语义推理正确或因果恢复，不将该私有复评分回传给模型。

训练候选门禁在结果前固定：完整32条及源码/轨迹审计通过，配对成功率差>0，
至少2个interactive-only成功配对，且上述反馈修复代理覆盖≥2个家族。它是开发
决策启发式，不是统计显著性或收益保证。未通过就诊断任务/模型/预算，不增加训练。

即使机制门禁通过，训练仍须有独立费用准入：沿用4B/rank16/G4/LR1e-5、当前
GRPO success-only，先8组/32条Pilot，另保留完整32条Dev Base/RL评测，不挤占评测。
新上限下单组最坏约1.91元（4条采样、48×6143 padded训练Token、优化/快照/状态
预留），8组+32条评测+初始化收尾约18.58元的保守准入额。目前余额不足此上界，
不自动加额；实际资源统计后可提出有证据的更紧估计，不能凭猜测释放历史资金。
正式test、24组训练及新Reward均在后续独立门禁之后，当前不启动。

## 入口与证据

```bash
uv run --project experiments/trainable_agent --extra lab python -m minidsh_rl.minirepo_gates \
  --output benchmark-results/trainable_agent/minirepo-v1/gates-new
uv run --project experiments/trainable_agent --extra lab python -m minidsh_rl.minirepo_experiment \
  --output benchmark-results/trainable_agent/minirepo-v1/base-interaction-new \
  --gates benchmark-results/trainable_agent/minirepo-v1/gates-05/summary.json \
  --quote benchmark-results/trainable_agent/minirepo-v1/service-preflight-01.json \
  --swanlab-mode online
```

输出目录必须全新；示例不会解除账本或门禁。Gate源文件变更需要重新离线验收。
分析入口`minirepo_analysis`不产生模型调用，只复评分已有候选。SwanLab派生视图
失败不覆写本地事实源；历史GRPO/GSPO轨迹与报告保持冻结。

## 开发修订：单次输出上限复核

首批32条已完整结束，single/interactive均1/16；保守占用1.982元，累计46.181元。373次真实采样中22次length终止、23次输出格式拒绝，9次达到上限的响应提及Patch。原判据和全部结果保留。

依据开发证据，新增独立开发复核：同8题、同2seed、同2模式，仅将单次输出上限512→1024；36k总Token、3072总输出、12步/工具、6k上下文、模型checkpoint、Prompt和评分不变。新32条上界仍2.88元，最坏累计49.061元；不增加累计50元上限，也不把新队列当作未见测试。源码比较记录确认仅两个配置传递模块变化，Prompt AST与其余源码hash一致。

这是一项待验证的截断瓶颈假设，不预设成功率会提升。新阶段替代当前训练候选，完成后只分析数据，不自动启动第三批或RL。证据在`output-cap-amendment-01.json`及`output-cap-source-proof-01.json`。
