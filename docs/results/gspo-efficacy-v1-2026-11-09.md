# GSPO 与 Base：同规模固定基准复测

2026-11-09；状态：完整352条真实轨迹与独立评分复核通过。

本轮测试观察到正向点估计，区间包含0，不能排除无提升。

| 部分 | Base | GSPO | 配对差值 | 95%家族聚类探索区间 |
| --- | --- | --- | --- | --- |
| 验证 | 10/32（31.25%） | 18/32（56.25%） | +25.00pp | [+0.00, +43.75]pp |
| 测试（主要终点） | 28/64（43.75%） | 33/64（51.56%） | +7.81pp | [-1.56, +17.19]pp |

完成与历史GRPO相同的40个G4训练组、160条训练轨迹；产生25次真实更新，15个全0/全1奖励组跳过。
训练题通过98/160仅描述采样奖励，不作为效果提升的证据。验证64条和测试128条均重新采样，
Base/GSPO分别32次验证、64次测试；测试GSPO独有成功12对、Base独有成功7对。
完整声明分母保留，无补采、挑题、根据验证换模型或缩减测试。

模型Qwen3.5-4B、LoRA r16、训练seed17、G4、Adam lr1e-5；复用原expansion_v2的24家族96题、
家族隔离划分、固定40题训练顺序、提示、工具、采样参数与独立二值测试奖励。
此处是函数级Python修复题，不与后续多文件v4学习曲线混用。Base取本轮初始零LoRA，GSPO取第40尝试组终点。

算法为 `gspo_multiturn_wide_clip_v1`：整轨迹assistant Token的几何平均ratio、轨迹等权、
clip=.2/.2、组中心优势、无std/KL。PyTRIO0.2.9自定义损失按已测CE sum还原；
复用历史数值探针前核对五个算法源码hash。不是论文默认窄裁剪的精确复现。

根项目1072项测试、实验包358项测试通过；Ruff格式/静态检查与两套严格mypy检查通过。

全部40组按原始Token/logprob重建奖励、优势、Datum hash与掩码，25个有效组重建当前loss/梯度，
更新回执链完整。实际初始权重与历史seed17初始归档差0；训练后权重差L2=0.265292063312。
使用独立判据核对每条轨迹，不以模型自报完成评分；容器隔离与真实副作用探针前后均通过。

运行中第31组反传结果查询发生EventPollingError，原Actor owned/ENDED，尚未提交该组optim。
保留原124条轨迹、30个确认组、19次更新和未知请求记录；新Actor加载最后完整optimizer检查点，
实际LoRA差0，非空Adam state和param_groups逐项全等后重建独立事务，未重采原轨迹。
两次检查点上传READY等待停点及源码快照保留，最终采用最多5分钟的只读就绪等待。
恢复后三个目录中的原始124条轨迹字节一致。该过程见[ADR0057](../adr/0057-gspo-terminal-backward-rollback.md)。

区间按家族聚类bootstrap10000次、seed20261106、nearest-rank；验证4家族、测试8家族。
重复采样不视为新的独立题。只有一个训练seed，旧测试集此前已打开，因此属于预声明固定基准复测，
不能称新盲测或稳定跨任务收益。本轮只比较Base与GSPO；不可用历史GRPO成功率直接证明算法优劣。

[冻结协议](../trainable-agent-rl/gspo-efficacy-v1.md) · [结构化结果](gspo-efficacy-v1-2026-11-09.json)。
本地原始证据：`benchmark-results/trainable_agent/gspo-efficacy-v1/run-03`，原失败和只读等待停点在run-01/run-02。
结果由`experiments/trainable_agent/scripts/gspo_efficacy_report.py`离线生成，脚本及输入hash保存在JSON；重新生成不代表新一轮真实实验。
