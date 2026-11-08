# 固定执行入口与数值组件复核：真实研究队列仍未准入

日期：2026-11-07；依据[ADR0046](../adr/0046-learning-execution-admission.md)。
本阶段完成prepare/inspect/run代码、固定配置及初始LoRA门禁的离线验收。
没有运行真实run、查询实时PyTRIO服务、创建真实SDK会话或新增模型/训练调用。
目标“交互可学习性，再比较算法”仍未完成，以下不是新模型效果。

## 1. 当前代码与执行顺序

| 模块 | 输入 → 输出 | 边界 |
| --- | --- | --- |
| `learning_components.py` | 归档源码、旧数值结果、实际旧权重 → historical_component_reuse | 从权重重新计算，不接受只有passed=true的摘要 |
| `learning_run.py` | tokenizer/报价/组件证据/数据 → execution-config与固定80行plan | 配置hash进入plan；prepare/inspect不访问服务 |
| `learning_initial.py` | 本次initial Sampler及旧参考结构 → 初始LoRA检查 | 真实入口必需；未通过不开始readiness |
| `learning_execution.py` | 完整资金与状态 → preflight hook → factory | 缺资金、未知更新或输入变化均在factory前停止 |

配置固定4B/rank16/seed17成功奖励GRPO profile、镜像digest、renderer/tokenizer、SDK
0.2.9及三个已安装SDK文件摘要，并绑定全部外部输入hash。不开放命令行超参数搜索。
prepare不代表授权；inspect始终execution_admitted=false，报告资源能力与未实测项。

显式run才会持有CalibratedBudget租约，先恢复可确认的控制状态、检查完整剩余资金，
再经before_factory检查只读实时模型/报价，重验配置/源码及资金后才创建ServiceClient。
报价类别/单位/单价不符合固定规则则停止；不是沿用旧报价盲目付费。每个Model请求
继续逐次预留，未知RPC保留费用、不隐藏重试。CLI只输出封闭摘要/错误类型，不打印
SDK对象、任意验证输入或可能含凭据的异常内容。

实时GET报价、真实初始导出及真实新任务尚未执行。当前run入口的存在不构成运行授权，
也不表示所有真实SDK行为已经验收。所有旧H1–H3判据和结果保持不变。

## 2. 为什么复用历史数值组件

实际核对`contracts.py`、`datum.py`、`trainer.py`、`pytrio_backend.py`与core-protocol-v2
归档字节一致；同时核对归档中的uv.lock和当前PyTRIO版本均0.2.9。重新读取旧Tensor
archive、计算weight_delta，并与旧结果交叉核对，而不是直接信任passed标记。

本地新派生`learning-execution-v1/component-review-01.json`得到：

| 检查 | 原权重重新计算 | 结论边界 |
| --- | ---: | --- |
| 全零Mask更新差 | 0 | 旧组件的实际数值证据 |
| 有信号更新范数 | 约1.24863e-7 | 旧线性化optimizer探针，不是本轮训练幅度 |
| duplicate / single | 0.99987171 | 原sample_mean重复不放大 |
| split / single | 0.50654590 | 原未补偿分段约减半，继续使用原补偿 |
| optimizer恢复相对差 | 0.02737819 | 原3%容差内，不是位级一致 |

旧结果、权重和源码未改；浮点复算的末位差使用1e-8相对/1e-12绝对比较，数值门槛
仍原3%。新证据明确origin=historical_component_reuse、new_provider_calls=0。
核验范围不是“新SDK适配重新实测”，也不能证明服务端永不漂移。关键代码、SDK版本、
renderer/tokenizer或输入改变则复用失效；重做探针须另核预算，不在现有组内临时补采。

当前SDK文件hash固定的是本次安装，不是历史远端引擎的位级证明。旧跨引擎1e-3假设
失败的限制保留；每个新组在真实backward返回后继续运行既有fresh-policy 5% clip-share
门禁，之后才允许optimizer，不因历史数值通过就取消这项检查。

## 3. 新的初始LoRA与准备额度

真实入口会在本次Train/Sampler初始保存后，以owned training run及Sampler URI查找
checkpoint，下载到控制面，校验配置、tensor名称/形状、有限值与全部LoRA B为零。
参考是已固定hash的旧初始archive，用于结构比较；不是把旧权重当成本次Base。
Base及RL仍从同一新建initial开始，Agent不可访问这些权重或私有参考。

检查组合预留0.02元，来自原0.50元额外准备额度，不是已证实提供商收费。
初始化固定0.21元加该项0.02元；Checkpoint记录preparation_spent，控制器从剩余
额外分配扣除一次。确认initial回执后崩溃再恢复，不重复扣除这笔准备额；恢复本身
另按原0.01元规则记录。初始文件和policy artifact进入状态hash保护。

失败时不发布initial-ready回执，不执行任务。下载或SDK结果未知仍保留pending，
不自动重放。当前只在小型本地safetensors及假SDK上验证此逻辑，本次真实初始导出
未发生；不把该门禁代码写成已测的零LoRA结论。

## 4. 已验收与资源状态

定向33项检查覆盖历史权重重算、错误passed标记拒绝、配置与输入变化、资金阻断、
preflight/factory顺序、零B与结构检查、准备金额边界和确认回执恢复不双扣。
后端/临时账本均为contract_fixture，不能计入科学样本；代码接线不证明模型可学习。

完整回归：主项目1072项（51.58秒）、实验包257项（21.42秒）通过；Ruff格式/静态
检查通过，strict mypy分别175/158文件通过。原始输出、命令及JUnit保存在本地
quality-gates-01，不能据检查数量补认未实测的新SDK路径或完整安全矩阵。

实际持有活动财务租约的入口阻断检查也通过：preflight_calls=0、factory_calls=0，
账本前后SHA均为`7e9712a184726e9f0013a3b28c5ebc5faee19bc4384f181366292dee8ce2dc7c`。
此项只调用资金拒绝路径，未调用真实run子命令或远端服务。

完整队列仍16条readiness、32条训练、32条配对test。固定总分配20.302元，原0.50
额度已包含上述0.02，不增加总额。活动账本保守占用42.667/50，可用7.333，缺口
12.969元、无pending。它是估价/已覆盖账单/预留的混合占用，不是最终实付。
没有因复用数值组件自动释放旧财务预留，也没有使用重置卡或提高上限。

## 5. 可复核命令与下一步

下列都是离线命令。已存在目录不可覆盖，重做使用新输出名；源码变化后用归档
复核或重新准备，不能改写旧hash。

```bash
uv run --project experiments/trainable_agent --extra lab python -m minidsh_rl.learning_run \
  review-components --root benchmark-results/trainable_agent \
  --output benchmark-results/trainable_agent/learning-execution-v1/component-review-new.json

uv run --project experiments/trainable_agent --extra lab python -m minidsh_rl.learning_run \
  inspect --directory benchmark-results/trainable_agent/learning-execution-v1/prepared-01
```

新准备命令的四个输入为固定tokenizer证据、已核报价、component review、metadata-v2
数据证据；示例目录的execution-config.json及plan已保存这些绑定。真实run会发起付费
动作，本阶段不调用，也不因入口准备完成自动执行。下一步优先核对可审计的历史
费用覆盖；若资金仍不足，保持80行未运行并明确资源选择，不缩小研究目标追求绿灯。
真实readiness通过之后才进入固定8组训练，再使用预声明test评估学习收益；算法比较
仍在学习证据之后，不混入当前阶段。

本地`audit_evidence.py`基于plan/config、归档源码、12项历史输入、JUnit及命令回执
重建新的manifest，不访问SDK、不改账本；锁文件和pytest目录别名不作为事实源。
重做时指定新名称：

```bash
python3 benchmark-results/trainable_agent/learning-execution-v1/audit_evidence.py \
  --output benchmark-results/trainable_agent/learning-execution-v1/review-02.json
```

contract-test-source.tar.gz保存六份相关检查源码；不把测试中的负例或模拟provider对象
当作真实研究运行。manifest只证明其声明范围内的本地输入完整性。
