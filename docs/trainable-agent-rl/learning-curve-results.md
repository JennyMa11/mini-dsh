# 单 seed 学习曲线：新数据判据门禁未通过

日期：2026-11-08。执行依据[冻结协议](learning-curve-plan.md)和ADR0050。
本阶段完成新数据与离线判据实测，**尚未执行学习曲线或验证RL收益**。
按预声明门禁停止后续付费阶段，没有临时换题、补采样或调整本版本判据。

## 实际交付与完整分母

30道同家族跨模块修复实例：12 train、6 dev、12 validation，三个算法组。
组合枚举、split、210候选与420项公开/私有检查清单均在执行前冻结。
210个case/probe实例实际55种唯一完整程序；不能当作210个独立问题。
模型未见private/reference，原默认白名单不变；curve_pure_v1显式允许issubset。

| 检查对象 | 声明实例 | 公开判据 | 私有判据 |
| --- | ---: | ---: | ---: |
| reference | 30 | 30通过 | 30通过 |
| 两个缺陷合并 | 30 | 30检出 | 30检出 |
| 常数错误解 | 30 | 30检出 | 30检出 |
| 单缺陷 | 60 | 60检出 | **56检出、4漏检** |
| 额外语义错误 | 60 | 60检出 | 60检出 |

**420/420真实禁网容器检查完成**，无超时或策略拒绝。所有错误检测都检查实际测试
执行，不以静态拦截代替。源码和历史财务账本在整个容器检查期间hash不变。
离线测试/类型门禁与这些任务判据分开记录。

## 漏检及结论

path_router_03、05、06、09的单缺陷探针把canonical的
`return '/' + path.strip('/')`改成`return path`，违反公开尾斜杠规范化合同。
四个实例对应**一种唯一错误程序/缺陷**，公开测试检出，私有测试却通过。
现有私有输入中的末尾斜杠组合不足以区分这一变更；这是本数据的判据覆盖缺口，
不是模型修复失败、RL无效或运行时隔离缺陷。未基于该结果改写原判据或旧分数。

新CLI的离线coverage_gate显式要求公开/私有两套判据均覆盖全部预设错误，
并拒绝缺失、超时、未执行或私有漏检；因此本版不准入后续模型队列。
新增纯函数门禁在420实测结束后开发，并依据原预声明要求判定；实测执行源码
archive与后续报告生成器hash分别保留，不将后续源码称作执行时源码。

## 未运行项目及预算

- 科学队列声明216条：dev24、train96、validation96，实际**0/216**。
- 新provider调用0、训练更新0、模型Token0；不是学习曲线零成功率。
- 预算迁移未执行，当前活动账本及累计50元限额未修改；用户已确认的新100元/
  累计150元档仍为后续待实现契约。无本阶段新增pending或费用。
- 曲线调度、checkpoint验证、真实dev准入、三seed与算法消融均未测/未执行。
- 本Goal按数据门槛失败交付停点；不将下游未实现功能计为已完成。

后续若继续，先独立冻结curve_dataset_v2的尾斜杠私有输入及完整探针，再重跑
公开/私有全部检查；不能覆盖v1或把v2检查追认为v1通过。本报告不启动下一轮。

## 证据与复核

本地`benchmark-results/trainable_agent/learning-curve-v1/audit-01/`含源码archive、
case/probe清单、420份原verdict、criterion-binding、stdout/stderr和完整summary。
[可提交JSON摘要](../results/grpo-learning-curve-coverage-2026-11-08.json)由下列命令生成；
输出必须是新路径，重算不访问模型，也不等于新实测：

```sh
uv run --project experiments/trainable_agent python -m minidsh_rl.curve_gates \
  --summarize benchmark-results/trainable_agent/learning-curve-v1/audit-01/summary.json \
  --output benchmark-results/trainable_agent/learning-curve-v1/recheck-new.json
```

## 覆盖矩阵

| 能力 | 触发/期望 | 证据 | 状态 |
| --- | --- | --- | --- |
| 新数据划分/不同缺陷组合 | 30题，12/6/12，无相同双缺陷跨split | case-manifest、契约测试 | 通过；共享算法不等于语义独立 |
| 判据版本隔离 | legacy拒绝issubset，新版接受，I/O仍拒绝 | test_curve_cases | 通过 |
| 公开错误反馈 | 180错误候选实际失败 | 420原verdict | 通过有限探针 |
| 私有判据覆盖 | 相同180错误候选实际失败 | 4漏检、摘要mismatches | 失败 |
| 运行可复核 | 来源/账本不变，420绑定齐全 | summary与verdict hash | 通过 |
| 无模型调用停点 | 数据不通过不进入provider | 本阶段只有离线入口，无预算迁移 | 通过 |
| 训练/曲线/学习收益 | 216预声明轨迹 | 无 | 未运行 |
| 新预算档/恢复 | 历史ID防重放、累计150/阶段100 | 无 | 未实现 |
| 通用Secret/网络/取消攻击矩阵 | 需独立真实副作用探针 | 本阶段未执行该矩阵 | 未测 |

## 完成检查

本工作树root Ruff format/check通过，strict mypy覆盖175文件通过，pytest1072通过；
实验包strict mypy覆盖154文件通过，pytest278通过。离线摘要使用新输出路径重算，
逐字段与提交摘要完全一致，420份verdict及相关绑定/输出hash核验通过。
检查结束无本实验残留运行容器。上述测试数不替代私有判据失败结论。
