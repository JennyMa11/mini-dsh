以下是独立玩具示例，不是当前仓库的文件或题解。仅示范工具格式和反馈利用。
每次响应只有一个tool_call；参数中的old必须在对应文件唯一匹配。实际任务必须
使用自己的规格、可写文件与工具Observation；不要创建或寻找本示例的toy.py。

假想任务：format_tag(text)应返回去掉两端空白后的大写字符串。假想工具交互如下。

Assistant:
<tool_call>{"name":"read_file","arguments":{"path":"toy.py"}}</tool_call>
Observation: {"content":"def format_tag(text):\n    return text.lower()\n"}

Assistant:
<tool_call>{"name":"apply_patch","arguments":{"edits":[{"path":"toy.py","old":"return text.lower()","new":"return text.strip().lower()"}]}}</tool_call>
Observation: {"ok":true}

Assistant:
<tool_call>{"name":"run_tests","arguments":{}}</tool_call>
Observation: {"exit_code":1,"stdout":"","stderr":"AssertionError: expected 'A', got 'a'"}

Assistant:
<tool_call>{"name":"apply_patch","arguments":{"edits":[{"path":"toy.py","old":"return text.strip().lower()","new":"return text.strip().upper()"}]}}</tool_call>
Observation: {"ok":true}

Assistant:
<tool_call>{"name":"run_tests","arguments":{}}</tool_call>
Observation: {"exit_code":0,"stdout":"public tests passed","stderr":""}

Assistant: 已按规格修改并通过公开测试，提交当前候选。最终正确性仍由独立验证决定。
