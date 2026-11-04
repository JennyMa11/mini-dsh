# ADR 0020：可编辑的终端输入适配器

- 状态：Accepted with limitations
- 日期：2026-10-03
- 补充：ADR 0007 的标准库行输入设计；ADR 0019 的输入缺字诊断。

## 背景

用户实际反馈打字不流畅，中文任务进入 Trace 时出现缺字。在本机 macOS / Python
3.12.14 的真实 PTY 中，用原来的 `input()` 输入“个人网页”、退格一次、再输入“页面”，
得到含 UTF-8 surrogate 字节的损坏字符串。该复现说明原行输入存在中文退格问题，
可以解释一种缺字路径，但不据此判定之前那条 Trace 的所有缺字都由该路径造成。

原输入没有显式字符编辑、历史或多行粘贴处理，输入质量不应交给模型补救。

## 决策

CLI presentation 增加 `terminal.py`，使用 `prompt_toolkit` 3.x 作为终端编辑器。
它是包的运行依赖，锁定安装版本及 `wcwidth` 传递依赖；不进入 domain、ports 或
Agent Loop，也不改变模型、工具、权限和预算接口。

真实 TTY 且 `TERM` 不是 `dumb` 时创建一个 PromptSession；其他输入表面使用原行输入，
支持管道、重定向和现有离线 CLI 测试。库 API 依据
[官方输入文档](https://python-prompt-toolkit.readthedocs.io/en/master/pages/asking_for_input.html)
与[按键文档](https://python-prompt-toolkit.readthedocs.io/en/master/pages/advanced_topics/key_bindings.html)。

- Enter 提交；Alt+Enter（或 Escape 后 Enter）插入换行。保留反斜杠续行。
- bracketed paste 中的多行文本插入同一缓冲区，后续 Enter 才提交。
- 左右键编辑、上下键回忆历史；固定命令通过 Tab 补全，唯一匹配同步完成。
- 补全不在打字时自动弹出，不触发模型或网络；输入阶段不运行 Agent Loop。
- 输入布局仅占草稿、换行和补全菜单需要的行数，限制绘制区域，避免终端剩余空间
  被渲染为整屏空白。提示显示在输入行右侧，正文增长时自动避让。
- 历史使用 InMemoryHistory，不读写用户主目录的历史文件。Key 继续走独立 getpass
  隐藏输入，不进入任务历史；原禁止明文回退的规则保留。
- 编辑器提交后保留文本显示，避免重复打印 `Request:`；普通行输入保留该回显。
- Ctrl+C 清空草稿并返回提示符；空输入 Ctrl+D 退出。编辑器释放终端输入模式后才
  执行工具或读取隐藏 Key。
- 工具审批临时显示审批问题及 `Enter: confirm`；允许、拒绝、EOF 或取消后恢复原
  聊天提示及编辑提示。`PromptSession` 会保留每次调用的提示覆盖值，必须在 `finally`
  中恢复，避免下一轮任务仍显示审批问题、误导用户把审批字母作为聊天发送。

## 替代方案

- 仅引入 readline：依赖平台后端差异，多行粘贴和显式按键行为仍需另外维护。
- 自己处理 raw mode、UTF-8、字符宽度与转义序列：实现和跨平台维护成本高。
- 完整全屏 TUI：会扩大本次修复范围，选择就地的输入编辑器。

## 验证与边界

使用官方建议的 PipeInput / DummyOutput 验证真实按键解析：中文退格和光标插入、
多行 paste、Alt+Enter、反斜杠续行、Tab、历史编辑、取消与 EOF；没有用 mock
的输入字符串代替按键处理。现有 CLI 测试验证普通输入回退与隐藏 Key 行为。
审批回归使用相同真实输入解析器验证 `y/n/a`、EOF 与异步取消后的提示恢复，以及
审批后下一条中文任务的正常提交。

另用 POSIX 真 PTY 运行默认适配器，验证中文退格后的实际返回值和取消后的输入模式
恢复。对比 termios 时忽略内核可能更新的 PENDIN 记账标志，其他模式与控制字恢复。
测试不发送模型请求，不依赖用户历史或配置。
另检查实际渲染几何：空草稿为一行，三行草稿为三行，中文长文本按字符宽度自动换行。
完整 CLI 的临时工作区 smoke 检查确认中文编辑与多行粘贴完整进入模型请求 Trace。

验收范围是字符编辑与提交链路；不同终端和输入法的候选框体验仍需实际使用验证。
多行粘贴安全提交要求终端支持 bracketed paste；不支持该能力的终端不能推定拥有
同样行为。普通行输入回退不提供 Unicode 编辑保证。Windows 尚未进行真实控制台
验收。模型输出仍按步骤显示，没有增加 Token streaming 或运行中任务编辑。

## 2026-11-04：状态栏与带说明的命令补全

用户要求提供更接近终端型 Coding Agent 的交互入口。裸 `mini-dsh` 已等价于 `work`；
本次不新增第二套解释器或全屏 TUI，只增强现有终端展示：

- 输入框底部状态栏显示模型、工作区、权限和上一轮结果；文本过滤控制字符并限制在
  终端宽度内。`status_provider` 只读取展示状态，不访问运行时、模型或工具。
- Tab 补全按整个草稿前缀匹配，菜单显示命令说明；唯一匹配仍直接补全。
- 工具完成行追加 `tool_call_completed` 事件中的 `duration_ms`；缺少该字段时不显示耗时。
- 首次任务缺少 Key 时，`/status` 说明会隐藏输入并请求配置。

状态栏只在输入提示激活时显示；任务执行期间仍是逐行事件输出，没有增加运行中刷新
或全屏布局。这是展示层改动，不改变 Tool Runtime、权限、预算、Trace 与 Session 语义。
离线按键测试验证补全菜单、状态栏过滤与截断；工具耗时测试验证使用真实 Observation 事件。
