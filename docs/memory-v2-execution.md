# Memory v2 执行记录

当前授权：用户在P0/P1交付与prove-it复核后，明确要求P2、P3、P4、P5。按阶段顺序实施，保留全部目标；本页记录当前源码开发状态，不代替阶段验收报告。

## 当前阶段：P5（协议与种子准备）

P2的渐进读取、P3的结构化笔记/历史、P4的手动整理已分别通过阶段验收，见[P2](results/memory-v2-p2-2026-11-11.md)、[P3](results/memory-v2-p3-2026-11-12.md)、[P4](results/memory-v2-p4-2026-11-12.md)。最新门禁为1261完整测试，真实模型调用仍为0。P5需要另行冻结配对协议与具体种子授权，尚未实施真实收益实验。

## 历史：P2完成后的P3开发起点

P2已通过[冻结阶段验收](results/memory-v2-p2-2026-11-11.md)：1118测试、245个Memory节点、MV2_01–20及32项真实OS/恢复判据通过。P2源码包和原始证据已封存；下方保留其开发过程。P3依据[新执行契约](memory-v2-p3-contract.md)推进，P4/P5未开始。下文P2缺口及P3领域首轮段落保留当时状态；当前P3进展见文末“存储与运行时开发”，不能把历史未实现清单套于当前工作树。

基线提交 `a7c5d3bb0eff990e701051e6262a62a82bfbf349`，分支 `feat/memory-system`。原P0/P1文档及checks JSON是其当时源码/文档的历史核验记录；其中“本地源码未改变”的复核断言不应套用于P2开发后的工作树。原始记录不回填成新源码验收。

已开发：确定性词法检索、无正文卡片、按修订/hash引用读取、私有来源复查、路径/错误适用性、独立输出Token上限、派生请求撤回、显式progressive_v2配置、两条组装入口和Context准备计账。通用默认模式与mini-dsh-work的v1配置保留。原匹配纯函数移到domain/memory_query.py；application路径保留兼容导出，避免runtime反向依赖application。

运行示例：`uv run python examples/progressive_memory_demo.py`。只在临时目录创建合成环境确认，运行外fixture_operator维护链提交，再检查搜索不含正文、按引用读取正文；真实模型调用为零。旧示例仍在examples/project_memory_demo.py。

P2配置在既有memory表内增加`retrieval_mode = "progressive_v2"`；省略仍为deterministic_v1。index_token_limit/search_token_limit/read_token_limit分别默认512/512/2048，允许下调，不允许上调。模型只能看到search_memory/read_memory/propose_memory中显式注册的工具；prepare_memory_index仅在Context执行器。

进一步的边界契约先复现了四类开发缺陷再修正：用户角色伪造调用、缺少执行状态、复用call_id导致错误因果配对、复用ID干扰旧正文撤回。事实匹配现在按顺序消费Assistant调用/实际具名Tool结果，只接受已执行状态；派生结果按当前完整引用和正文内容核对。主动搜索命中启动索引前五条之外的记录也有回归：最近一次搜索的至多五张卡片独立保留并在下次准备重新核验，不能因为不在启动目录中而消失。

一个prepare最多分别复查启动卡片、最近搜索卡片、选中正文各五条（重复引用可重复核验，均在同一次有界工具执行内，整体5秒超时）；没有为索引外搜索结果维护无界缓存。只有卡片/正文满足既有权限和来源检查才可继续暴露。

开发证据在`benchmark-results/extension-v1/memory-v2/p2-development/`：attempt_0的源码清单和gates.json记录当时四门禁；native_world_0记录默认Docker后端正向探针，interrupt_timeout_0/interrupt_cancel_0各验证真实独立session子进程、整个容器停止/移除、宿主库不变及维护守卫释放。attempt_2/native_world_2记录已知宿主路径的实际读写拒绝、绝对文件工具拒绝、合法工作区操作、工作区唯一挂载、1 CPU/1 GiB、禁网、cap-drop/no-new-privileges及索引/正文读取。

attempt_1保留检查器错误：默认容器根文件系统只读，攻击程序创建`/Users`被EROFS拒绝，原检查器却要求创建成功后输出JSON。没有放开隔离或修改生产代码，attempt_2改为检查实际读取/写入errno并保留原失败回执。这是检查器错误，不是宿主写入成功。以上仍是开发探针；后续源码修复后的最终P2证据需重新冻结，不能把旧source hash当新版本验收。

先写tests/test_memory_discovery.py、test_memory_progressive.py的契约并观察缺少模块的红灯，再实现对应模块；开发发现的混合重音词分割错误已由多语言契约拦住并修正。

两条入口×七种Context策略的开发检查发现，retrieval策略可能在首次模型请求前真实读取目标文件；这不是未执行调用授权绕过。原测试错误地假定所有策略都没有预读取，六个分支失败；现以原始Trace里实际成功读取的调用/结果顺序判定适用性，保留完整Loop测试。另有未导入RuntimeComponents的三个夹具错误和局部mypy/lint错误，均在定向复验中处理，不计为机制收益或正式功能验收成绩。

P1规格的规范化澄清（不改变选择目标）：目录scope必须用`src/db`，不是带尾斜杠的`src/db/`，后者被既有MemoryEntry路径契约拒绝；连续非ASCII字母与ASCII字母组成的词（例如échec）保持一个词项。运行内已执行的受控仓库检索和显式模型工具同样能提供匹配事实，只有未配对/未执行调用不能提供事实。这些说明须随最终P2夹具固定，不能以未运行的规格表冒充完成。

## P2历史缺口（已由阶段验收关闭）

最新开发复核见 [P2 prove-it](results/memory-v2-p2-prove-it-2026-11-11.md)：冻结源码的四门禁通过，1090测试、81项Memory定向case、32项OS/恢复判据和两个示例通过。该记录明确列出未覆盖子输入，不等同于P2阶段通过；历史attempt和首次lint失败均保留。

MV2矩阵仍未逐行签署；当前开发测试只是部分分支证据。未批准与异项目的真实存储、Conversation跨run、输入拒绝与请求敏感值保护已有开发测试。新增三条完整Loop检查：受信的第二Store实例修订/拒绝条目或实际修改来源bytes后，原先已暴露正文从后续实际ModelRequest撤回，原始工具结果和Trace保留。仍须逐项核对所有子输入、默认关闭对照，以及最终源码的冻结、契约/OS证据归并和四门禁。真实存储线程取消、预算停止和Docker正反探针已有开发证据，尚待按最终冻结版本复验。

P3已进入独立契约/ADR阶段，见[ADR 0032](adr/0032-structured-memory-notes-and-history.md)；结构化笔记、历史、人工替代/合并、迁移及完整验收尚待交付。

P4未开始：手动consolidate、单Agent顺序、独立预算，仅产生提议，不自动批准或修改原始事实。

P5未开始：冻结配对任务、种子、判据、重复与总预算；复用runner，独立验收，分开报告功能与收益。真实种子仍必须有绑定具体内容/修订的操作者批准，不把开发fixture_operator当作用户授权。

后续只推进当前阶段必需工作，P2通过后再实施P3，直到完整目标P2–P5实际交付或出现有证据的外部阻塞。


## P2 补足验收开发（prove_it_1 之后）

补足共同夹具首请求的三张完整卡片；五候选中的来源 bytes 变化和独立 grant 撤销且不递补第六条；两项读取权限逐个拒绝；五条最大正文和单卡片上限；Store/来源故障与下一run恢复；错误筛选不能授予适用性；关闭时禁止初始化Store，以及两条入口连续两run的默认v1/显式v1完整请求一致；运行中新增记录不并入冻结快照。

新增回归先复现：P2事实事件没有携带排除原因；已登记禁止材料在propose的topic/path可以写入，正文虽拒绝也已进入模型响应Trace。现在事实事件带有有界omissions/剩余数/Token裁剪信息；直接提议统一检查元数据；v2显式登记禁止hash时在Model port装饰器中拒绝整个含禁止材料的响应，保留用量/unknown标志和安全错误分类，不保留该响应的动作或正文。流式文本在最终检查前不显示，以免拆分文本绕过检查。未登记hash时不增加该装饰器；通用默认仍关闭。

开发夹具错误保留：共同夹具第一次运行遗漏enabled_tools而被既有OS门禁拒绝；210 Token不够一张卡片与完整省略说明，改为预声明300 Token后可容一张；兼容对照初版误用configured构造函数的components参数并重复run_id，被类型和Session独占创建契约拒绝，修正为禁止Store初始化探针和唯一run_id。新增临时装饰器一度丢失安全错误code，回归红灯后修复。原始生产隔离与预算没有放宽。

## P3 首轮领域实现边界（历史记录，2026-11-12）

已先写25条domain契约、观察缺少模块的红灯，再实现MemoryNote/MemoryNoteSource：规范JSON、严格版本/字段/重复键、全包裹2KiB、例外/来源上限、来源与替代关系批准绑定、派生主张不复用旧run/证据的纯函数。MemoryEntry.revise可显式换来源run，同时递增修订并撤销旧授权；省略保持旧语义。

这些只证明N01–N04的部分domain分支。validate_note_evidence尚未接入运行时维护与来源查验；没有新工具/CLI注册、数据库迁移、历史查询、人工合并或原子替代。N05–N12仍not_run，不能把纯函数测试当作可用的P3安全链。P4/P5尚未开始。

P3第一次全仓门禁出现1条文档失败（其余1142通过）：更新测试数量的宽泛文本替换误改了README历史金额。现有test_readme_real_model_ablation_numbers_match_the_archived_runs准确拦截；恢复归档值后在domain_1重新执行门禁，不覆盖domain_0回执。后续数量更新只匹配中文数量声明，历史表值不参与替换。

## P3 存储与运行时开发（2026-11-12，阶段未出关）

已实现显式SQLite物理version 1→2迁移、hash命名私有备份、当前状态迁移快照、追加历史/分页/历史committed修订查询、批准绑定的原子替代，以及直接单条replace不能孤立supersede的门禁。旧v1读取不迁移；新空库可显式初始化；8MiB共享文件上限、每项目100条当前记录不淘汰。真实历史增长至8MiB附近后写入拒绝，当前记录和历史均未部分发布。

结构化提议与四项运行外维护（migrate/history/merge/revise-note）已接入服务；六项原维护链复用新的笔记来源/证据校验和原子提交。旧证据不能验证合并主张，source关系通过同项目/版本的实际历史或当前committed引用复查；原始材料不能包装进trigger/rule/verification/exceptions。普通propose/revise不能借JSON正文绕过结构化工具的读取权限与历史要求。

local/configured两条入口×七种Context策略，已覆盖Actor提议→运行外明确验证/批准/提交→新run读取→Conversation恢复。提议文本仍作为Actor自己的事实历史保留；它不进入批准知识投影。迁移、历史、合并和修订工具不注册给模型。

新增mini-dsh memory命令只组装操作者工具，不创建模型；显式operator、action和有界JSON参数，每次落私有操作Trace。既有deny_tools对操作者同样优先，输出服从配置tool_output_limit。

开发失败保留：最初历史API/笔记工具模块缺失的红灯；fixture异步all生成器、漏导入replace、重复parametrize装饰器和类型/lint错误已在定向检查修正。曾错误断言Actor自己已写出的提议不能出现在事实历史，现改为检查proposed状态、无批准及无知识投影，没有删除事实来迎合判据。

仍缺：P3离线完整示例、旧适配器拒绝/关系边界的覆盖签署、最终源码冻结与逐行N01–N12证据归并、迁移后数据库/备份的真实OS探针及阶段门禁。当前组件开发不代表P3通过；P4/P5尚未开始。

## P3 发布边界补充复核（storage_runtime_0之后）

storage_runtime_0的1202/84通过只适用于其冻结快照。继续细化N06时，在before_storage_publish检查点复现了取消后仍发布新库的缺口；底层_publish现将cancelled设为必传参数，所有五个发布入口由mypy检查，并在原子文件替换前集中复查。before_migration_publish和before_storage_publish两处分支均验证取消、线程收束和原库不变。发布后的取消或通知失败另验实际库已更新、历史可读及重复迁移无写入，不能报告为回滚。

另复现物理version2缺失历史表却被当作“已迁移”成功返回的问题；现在检查必需历史列，损坏库拒绝而不重建/备份。以上均先观察真实回归红灯再修复，旧快照不覆盖。P3仍未阶段签署，最终例子/OS/矩阵归并保留。

## P3阶段签署（2026-11-12）

[P3阶段报告](results/memory-v2-p3-2026-11-12.md)记录冻结源码、335/335 Memory节点、1208完整测试、37/37迁移后原生判据和三个示例。四门禁通过，真实模型调用0。attempt_0嵌套JSON检查器失败及attempt_1修正后的全量OS复验均保留。此前“仍缺/未验收”段落是对应开发快照的历史，不覆盖本次结果。当前进入P4契约阶段；P4/P5仍待交付，不能将P3功能通过解释为Memory收益成立。

## P4阶段签署（2026-11-12）

[P4阶段报告](results/memory-v2-p4-2026-11-12.md)记录显式运行外整理、输入hash与来源绑定、逐准备/写入前来源复查、仅proposed变更集、私有审计和按run读回。最终源码四门禁通过，1261完整测试、388/388 Memory节点、37/37原生回归及四个示例通过，真实模型调用0。

开发先观察缺模块红灯；严格布尔声明回归复现Literal[True]接受数字1，已改为显式identity校验。CLI首次遗漏注册后再选择的顺序被完整命令测试拦截。发布后通知失败以真实存储读回纳入候选，不自动重试；取消前后和已完整提议后取消均保存实际证据。首轮全绿后的覆盖审计又复现--script仍进入provider选择的缺口；保留红灯与首轮回执，最终attempt_1重新冻结并全量复验，不复用旧成绩。

当前阶段进入P5：尚待冻结任务、模型/采样、单变量对照、种子/审批、独立判据、至少三次配对重复、总Token预算和完整分母，再复用现有runner进行真实实验。已定位历史M5种子在原工作区，但旧队列批准绑定旧协议，不能自动当作新协议的授权，更不能替代新整理主张的验证/批准。历史收益结论不改。

## P5执行器开发与证据复核（2026-11-12，阶段未验收）

已冻结[配对协议](../benchmarks/memory_v2/p5-protocol.json)与[P501–P508契约](memory-v2-p5-contract.md)：六题、五组、每组三次共90槽位，统一任务预算，关闭Skills/Verification，复用现有隔离执行器与独立评分器。新增脚本支持源码包、运行外整理审阅、具体hash授权、顺序执行及只读派生报告；synthetic_fixture不能发起真实队列。失败消耗和未知用量保留，取消后保留候选及独立验证结果再向调用者传播取消。

开发attempt_0–2分别暴露2KiB笔记包超限、审阅包缺少计算hash字段、配置frozenset无法JSON编码，均保留原始失败。attempt_3的90/90脚本Docker演练完成，但仅适用于其冻结源码`260c30513270c9dbe8dcfe1cdb00a0ae1e505bfb9263d817f7a6eae7cd85ee1c`，真实模型调用0，不能证明Memory收益。之后修正取消产物丢失、各组正文暴露统计、严格标量校验及精确密钥扫描，旧演练不充当当前源码的阶段验收。

当前复核报告另存[prove-it记录](results/memory-v2-p5-prove-it-2026-11-12.md)，以具体源码包和本次命令日志为准。P5仍未验收：当前源码完整流程复演、专项安全/覆盖矩阵归并与真实配对实验尚待完成；真实实验还需要本机DeepSeek凭据，以及生成真实整理提议后对具体正文、修订和hash的新批准。历史M5批准和开发fixture身份均不能替代。

## P5统一preflight（当前待签署的离线范围）

新[覆盖矩阵](memory-v2-p5-matrix.json)固定P501–P508的输入、禁止行为、检查入口、预算与证据位置，执行前保持not_run。preflight现顺序执行完整四门禁、逐节点30秒组件契约、临时源码重建与frozen/offline安装、13项机制/故障用例、3项真实OS用例及90槽位脚本队列，再生成绑定全部文件的回执。真实整理入口和真实队列入口均核对该回执；绿色任务成绩不能替代原生安全证据。

开发回归复现并修复UsageObservedModel遗漏audit能力透传，以及可单独改写种子preparation_provider/manifest hash而未交叉核对的问题。真实准备在provider/输出目录前检查preflight与已有尝试，输入扫描后独占预留一次整理；失败/取消记录终态和已知用量。受控Model port的本地测试不调用真实DeepSeek，也不把其provider分支标签当作真实服务证据。

开发原生attempt_0因审计workspace未创建被真实Store路径守卫拒绝；新目录attempt_1修正夹具后复验十项机制/故障与三项OS用例。随后补充实际整轮控制器的unknown/全轮超额/cancel三用例：均只尝试一个槽位，保留全部90机会、独立判据及原始消耗；超额判违反，未知/取消保持预算未知，不将故障注入Token冒充API消耗。源码重建已在新临时目录用冻结依赖离线运行13个arm契约，不依赖HEAD或网络获取内容。

最终冻结版离线签署另存[preflight报告](results/memory-v2-p5-offline-2026-11-12.md)，不覆盖前轮prove-it或开发快照。P508真实实验与新种子批准仍是独立待办；离线ready不代表P5阶段通过或Memory有收益。
