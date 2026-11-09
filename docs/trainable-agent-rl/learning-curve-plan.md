# 单 seed GRPO 学习曲线：冻结实施协议

日期：2026-11-08。用户选择新增100元上限、首个Goal只验证固定配置可学习性、
先做同家族曲线、24组G4，在0/8/16/24组验证。范围决策见ADR0050。

## 队列与不可变条件

新curve_dataset_v1：path_router、dependency_batches、table_join，每组4 train、
2 dev、4 validation；30题来自不同跨模块缺陷组合，不只是改名/常数。
参考算法与既有家族共享，属于同家族新实例；缺陷原语存在重用，不保证语义独立。
组合枚举后以20261108固定shuffle，每族前4 train、中2 dev、后4 validation；
缺陷组合与历史两种双缺陷不重复。30×7候选×公开/私有=420次容器检查。
每题reference、broken、constant、2单缺陷、2额外语义探针在执行前冻结。

curve_pure_v1仅增加issubset；新README声明限制，旧白名单/结果不变。
public/private的具体输入分别保留，private/reference只在控制面。

Qwen3.5-4B/r16/seed17/lr1e-5/G4/clip.2，A=r-mean、不除std、无KL；
assistant-token mean。固定tool_example、Full History、batch_read_eight。
12步/工具、36k Token、3072总输出、6144单请求总长、1024单次输出、180秒；
独立禁网arm64容器、1CPU/1GB。判据容器1CPU/256MB/8秒。

## 阶段门禁

1. 420检查全部reference通过、错误解由功能测试检出；来源与历史账本hash不变。
2. dev6×G4=24：至少3混合组、至少2算法组，总成功6–18；至少2算法组具有
   公开失败→后续采样→新Patch→独立成功。未通过不训练、不补采样。
3. train12×2轮×G4=96；seed17固定轮次shuffle。全同组占槽、跳更新；
   连续10组零Advantage终止。每8尝试组保存与验证，不按有效更新计数。
4. validation12×2重复×4检查点=96；固定配对seed和顺序平衡，终点固定24槽。
   总计216条。主门槛：终点差>0且至少2算法组改善；仅支持后续复核。

## 分析、预算与停点

记录完整声明与已检查分母，配对成功率、逐算法组、有效更新比例、全部Token、
格式拒绝、反馈恢复、工具与端到端延迟。family bootstrap 10000次、seed20261108，
仅3簇局限。负mean sampled logprob只标surprisal代理，完整熵不可用则未测。
fresh-policy clipped share>5%、NaN、未知RPC、安全或来源问题立即停。

新增100元硬上限和累计150元名义上限需显式迁移；原历史账本不可改写。
完整队列按所有组更新的上界准入，包含数值/保存/审计和至多两次已知恢复。
数据未过门禁时不迁移资金、不连接provider、不启动训练。

## 执行记录

当前实现离线数据和显式判据版本；420项实际验收尚待运行。曲线调度、预算迁移、
开发准入及学习结果尚未完成。后续以独立结果报告更新，不以规划视作已实现。

## 本版本执行结论

420/420容器检查完成，私有判据漏检4个尾斜杠单缺陷实例（同一种程序）。
数据门槛失败，后续216条模型轨迹0条运行，无新增费用或预算迁移。
详见[完整停点报告](learning-curve-results.md)；本节更新此前“尚待运行”时点，
原冻结数据、判据和实际执行源码archive保留。
