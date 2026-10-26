# Echoic 前端设计概要（frontend 实现契约）

参考截图：飞书妙记风格会议详情页（最终实现效果见 `docs/screenshots/03-detail-viewpoint.png` 与 `05-mindmap.png`）。**详情页是最高优先级，要求 1:1 神似**；其余页面保持同一设计语言。

## 1. 视觉语言

- 浅色主题，主色 `#1677ff`，页面底色 `#f5f6f7`，卡片纯白，圆角 12–16px，细边框 `#eceef1`。
- 会议内容卡片使用浅灰绿底 `#f2f7f4` + 边框 `#d9e9df`（观点树分组卡片），正文深灰 `#1f2329`，次要文字 `#646a73`。
- 左侧图标栏 64px（浅灰底 `#f7f8fa`，图标 20px，激活项浅蓝底圆角），顶栏 56px 白底/毛玻璃，右侧 AI 面板约 400px，浅灰底 `#f7f8fa`，与主区以 1px 边框分隔。
- 字体系统栈：Inter / PingFang SC；标题字重 600；正文 14px，次要 12–13px。
- 图标：`@ant-design/icons`；头像使用圆形色块 + 姓名首字（不引外部图片）。
- 不使用外网字体/CDN，所有资源本地打包。

## 2. 路由

| 路由 | 页面 | 要点 |
|---|---|---|
| `/login` | Login | 品牌区 + 登录卡片；demo 凭据提示（admin/admin123）；成功后跳 `/` |
| `/` | Library 资料库 | 顶部欢迎/统计卡（录音数、总时长、本周新增、处理中）；搜索框；筛选（全部/已完成/处理中/失败，标签）；视图切换列表/卡片；上传按钮；录音卡片网格。空库时展示“上传第一条录音/开始录音”引导 + demo seed 提示 |
| `/record` | 录音与上传 | 左右两栏：麦克风录音（计时、暂停、电平动画、设备选择、重新录制、保存=上传）+ 拖拽上传区（多文件、进度）；下方“最近上传”列表轮询处理进度 |
| `/r/:id` | 录音详情 | **核心页面**，见 §3 |
| `/settings` | 设置 | Tab：AI 服务（ASR/LLM/Embedding 表单 + 测试连接 + 演示模式提示）、提示词、系统（上传限制/默认语言）、账户（修改密码）、关于 |
| `/search` | 全局搜索（也可顶栏弹层） | 搜索录音标题/转录；结果分组“录音 / 转录片段”，片段点击跳详情并 seek |
| `*` | 404 | 简单引导 |

全局：未登录 401 → `/login`。所有请求 `credentials: 'include'`（已封装在 `src/api/client.ts`）。

## 3. 详情页布局（对齐截图）

```
┌ 左图标栏 64 ┬────────────── 主内容区 ──────────────┬──── AI 面板 400 ────┐
│ logo        │ 面包屑：首页 / <title>   [下载][分享][…]│ AI问答        [收起] │
│ 资料库(激活) │ ┌──────────── 播放器卡片 ───────────┐ │ ┌─────────────────┐ │
│ 录音        │ │ 00:00 ━━━━━━━━━●────────── 40:37 │ │ │ 思维导图 | 关键洞察 │ │
│ 搜索        │ │ [发言人 avatars +3]  ⏪15 ▶ 15⏩ 1.0x│ │ └─────────────────┘ │
│ 标签        │ └───────────────────────────────────┘ │ 猜你想问             │
│ 设置        │ 转写 | AI纪要 | 观点树 | 章节          │  ✏ 帮我提炼…         │
│ (底部用户)   │ ┌──────────── 内容卡片 ─────────────┐ │  📄 ICPE论文怎么分工？│
│             │ │ …（按 tab 渲染）                   │ │ [消息流 + 输入框]    │
└─────────────┴──────────────────────────────────────┴─────────────────────┘
```

### 播放器卡片
- 白色圆角卡片，进度条顶部（主色，拖动 seek），左侧时间 `00:00` 右侧总时长；下方一行：发言人头像组（最多 4 个 + “…”）、中央 `↺15 / ▶ / ↻15`、右侧倍速菜单（0.75/1.0/1.25/1.5/2.0）、更多菜单（下载原文件、导出转录稿）。
- 使用原生 `<audio src="/api/recordings/{id}/media">`；`timeupdate` 驱动进度；支持拖动、±15s、倍速、`currentTime` 与转录/章节/观点节点联动 seek。播放中高亮当前转录段落并自动滚动。
- 无媒体文件（如仅演示元数据）时显示占位与禁用控制。

### Tabs
1. **转写**：按说话人分组的对话流（说话人名 + 颜色点 + 时间戳 + 文本），当前播放段高亮；顶部工具条：搜索转录、说话人重命名（点击说话人名弹出输入）、导出菜单（TXT/SRT/Markdown/JSON）；段落可编辑（内联编辑保存 PATCH）；点击时间戳 seek。
2. **AI纪要**：渲染 summary markdown（`react-markdown` + `remark-gfm`），下方“关键洞察”（Insights 卡片）、“待办事项”（AntD Checkbox 列表，本地标记即可）、“关键词” Tag 云；生成中骨架屏；未生成时显示“重新生成”按钮。
3. **观点树**：左侧根主题 pill + 纵向分组卡片（对齐截图：主题卡、分组框、圆点观点行、缩进）；节点 hover 显示时间戳/说话人，点击 seek；`kind=topic` 分组卡片浅绿底，`kind=point/task/risk` 行式排列，task/risk 有彩色标签。
4. **章节**：时间轴列表：`00:00–03:20 章节标题` + 摘要 + bullets，点击跳转；当前章节高亮。

### 右侧 AI 面板
- 顶部 “AI问答” 标题 + 图标按钮（收起面板、清空会话）。
- 两个大按钮：“思维导图”（蓝底浅蓝）打开全屏 Modal：React Flow + dagre 布局渲染 viewpoint_tree，节点按 kind 着色，滚轮缩放、fitView、可点击节点 seek；“关键洞察”（紫底浅紫）打开 Modal：洞察卡片 + 待办。
- “猜你想问” 标签列表（来自 suggested_questions，最多 4–6 条），点击直接发送。
- 消息流：用户消息右对齐蓝色气泡；AI 消息左对齐白底，Markdown 渲染，引用以 `[12:05]` 形式标注并可点击 seek；流式输出带光标动画；空态显示欢迎语和 3 个示例问题。
- 底部输入框（多行 TextArea + 发送按钮，Enter 发送 / Shift+Enter 换行），流式期间禁用发送并显示停止按钮（abort）。
- 面板在宽屏常驻；<1280px 可收起为悬浮按钮打开 Drawer。

### 处理中 / 失败 / 演示态
- 状态非 ready：内容区展示处理进度卡（stage 中文 + 进度条 + 说明），每 2s 轮询 `GET /api/recordings/{id}`，完成自动刷新 insights/transcript。
- failed：错误信息 + “重试处理”按钮（POST reprocess）。
- `settings.system.demo_mode=true`：页面顶部细条提示“演示模式：AI 结果为内置示例数据，可在设置中配置阿里云百炼等模型”，可关闭（sessionStorage）。
- 缺少 AI 某部分：对应卡片显示“生成中/未生成 + 重新生成”按钮（POST insights/regenerate）。

## 4. 资料库页

- 顶部 hero：标题“我的录音库” + 副标题 + 主按钮“上传录音”/“开始录音”。
- 统计卡 4 个：录音总数、总时长（小时）、本周新增、AI 处理中。
- 筛选条：搜索（防抖 300ms，调 `GET /api/recordings?query=`）、状态 Segmented、标签 Select 多选（单选也可）、排序、视图切换。
- 卡片：标题、日期、时长、标签、说话人头像组、状态徽标（处理中显示进度条）；hover 出现播放按钮；失败卡片显示原因。点击进详情。
- 空态/加载骨架/错误重试。

## 5. 录音与上传页

- 录音：`navigator.mediaDevices.getUserMedia({audio})` + `MediaRecorder`；波形/电平用 WebAudio AnalyserNode + canvas；计时 mm:ss；暂停/继续、重置、保存；保存生成 Blob（webm/opus，Safari 用 mp4）→ FormData 上传（source=browser_recording）。
- 上传：AntD Upload.Dragger 或自定义拖拽区，支持多文件顺序上传，显示每项进度与结果；上传完轮询状态。
- 权限失败提示（HTTPS/localhost 限制说明）。

## 6. 工程约定

- React 18 + TS strict + Vite；状态：TanStack Query（服务端数据)+ 少量 Zustand（auth/ui）；路由 react-router-dom v6。
- 组件目录：`layouts/AppShell`；`components/player`；`components/detail`；`components/library`；`components/common`（PageHeader/StatusBadge/EmptyState/AvatarGroup/MarkdownView）；`api/*.ts` 按资源拆分。
- 所有字符串中文；时间格式化 `mm:ss`/`hh:mm:ss`；日期 `YYYY-MM-DD HH:mm`。
- 必须通过 `npm run build`（tsc + vite）；不引入超重依赖（已装：antd、@tanstack/react-query、@xyflow/react、dagre、react-markdown、remark-gfm、dayjs、zustand、axios）。
- 移动端基础适配：≤768px 单列、AI 面板 Drawer、左栏收为底部导航或隐藏。
- 可访问性：按钮 aria-label、键盘可操作、对比度达标。
