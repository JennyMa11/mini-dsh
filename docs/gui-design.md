# GUI 视觉与交互

本地浏览器 GUI 参考 [DSH Desktop](https://github.com/anywhere-labs/dsh-desktop)
公开的 [首页截图](https://github.com/anywhere-labs/dsh-desktop/blob/faefc5b8504067164df498298e34ec0e561d6eee/assets/desktop-home-zh.png)
与 [会话截图](https://github.com/anywhere-labs/dsh-desktop/blob/faefc5b8504067164df498298e34ec0e561d6eee/assets/desktop-chat-zh.png)。
参考版本：`faefc5b8504067164df498298e34ec0e561d6eee`。

布局采用深灰双栏、工作区与当前会话导航、蓝色强调、首页居中的任务输入框。
进入会话后输入框固定在底部，用户消息靠右，回答靠左；“对话 / 轨迹”标签切换
消息和执行事件。运行用量在输入框下方展示，模型、权限、Context 和 Trace 路径
收进“运行设置”弹窗。侧边栏可收起，窄屏使用可展开导航。

“＋”提供真实的任务建议，发送支持 Cmd/Ctrl + Enter；回答可复制。
工作区改动仍调用已有 Git 工具，恢复会话仍输入当前工作区的 Run ID。
运行设置支持直接输入 API Key、切换 Multi-Agent、设置累计子 Agent 数量与并发上限。
Key 输入不回显，不存入浏览器存储或配置文件；留空保留当前凭证，关闭服务后重新输入。
保存设置仅影响后续运行，保留当前对话；运行期间禁用设置。模型与权限仍沿用启动配置。
Sub Agent 卡片显示角色、状态、结果、运行 ID 与用量，窄屏自动排列。
GUI 保持单工作区，不提供参考应用的多工作区、插件市场或桌面系统功能。

HTML、CSS、JavaScript 和 SVG 图标独立实现；没有复制上游源码、品牌标识或图片。
核心 Runtime 与权限契约保持 ADR 0008 的设计；协作与凭证设置边界见 [ADR 0011](adr/0011-gui-runtime-settings.md)。
