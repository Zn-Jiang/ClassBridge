# 🎨 家校沟通客户端 UI 说明文档 (UI Design Spec)

## 1. 视觉风格指南 (Visual Style)

- **设计规范：** Fluent Design (Microsoft)
- **组件库：** [PyQt-Fluent-Widgets](https://github.com/zhiyiYo/PyQt-Fluent-Widgets)
- **窗口效果：** 启用 `Mica`（云母）或 `Acrylic`（亚克力）背景效果。
- **色彩：** * **主色调：** 系统主题色（Accent Color）。
- **紧急消息：** 使用红色（Critical/Error color）高亮。
- **已撤回消息：** 灰色、低透明度。

---

## 2. 窗口结构 (Window Structure)

### 2.1 主窗口 (MainWindow)

- **容器：** `FluentWindow`
- **导航栏：** `NavigationInterface` (侧边靠左)
- **标题栏：** 自定义标题栏，显示应用名称及连接状态。
- **状态文本：** 在应用名称后紧跟一个 `CaptionLabel`。示例：`应用名 - [🟢 已连接 / 🔴 离线 / 🌙 考试模式]`。

### 2.2 系统托盘 (System Tray)

- **图标：** 显示应用 Logo，为同目录下的icon.png，我也提供了icon.ico，如果后面需要的话可以用。
- **状态变化：** 当有新普通消息且在非弹出时间时，图标闪烁；考试模式下图标叠加“静默”小图标。
- **右键菜单：** `显示主界面`、`退出`。

---

## 3. 页面详细定义 (Page Details)

### 3.1 未读消息页 (UnreadMessagePage)

展示家长发来的尚未确认的消息。

- **核心组件：** `SingleDirectionScrollArea` 嵌套一个垂直布局。
- **消息卡片 (`MessageCard`)：**
- **容器：** `CardWidget` (带阴影和圆角)。
- **顶部（分别排于最左侧及最右侧）：** `SubtitleLabel` (显示发送者姓名) + `CaptionLabel` (显示时间)。
- **中部：** `BodyLabel` (消息正文，支持自动换行 `setWordWrap(True)`)。
- **右下角：** `PrimaryPushButton` (文字：“已读”)。点击后触发已读回执并销毁当前卡片。
- **左下角：** 增加一个醒目的红色角标或文字标签显示消息重发次数，如 🔥 重发 3 次，如果没有重发，则不显示。

### 3.2 历史消息页 (HistoryMessagePage)

展示所有历史记录，按时间倒序排列。

-**历史消息页顶部：** SegmentedControl（分段控件）或 ComboBox，选项为：全部、普通、紧急。

- **核心组件：** 同未读消息页，但卡片样式略有区别。
- **状态标识：**
- **普通：** 正常显示。
- **已撤回：** 卡片背景变暗，正文添加中划线或显示“（家长已撤回该消息）”，不显示内容。
- **无按钮：** 历史页不提供“已读”按钮。

### 3.3 设置页 (SettingPage)

- **核心组件：** `SettingCardGroup` 配合各种功能卡片。
- **分组 1：状态管理**
- `SwitchSettingCard`: 考试模式。
- **分组 2：网络与同步**
- `EditTextSettingCard`: 服务器地址/端口/Token。
- `PushSettingCard`: “立即校时 (NTP)”、“刷新时间表”。
- **分组 3：消息与提醒**
- `ComboBoxSettingCard`: 历史消息保留时长（1月/3月/1年/永久）。
- `SwitchSettingCard`: 课间自动弹出主窗口、紧急消息声音提醒。
- `EditTextSettingCard`: 紧急消息的稍后提醒的默认时间（用输入框输入，单位为分钟）。
- **分组 4：常规**
- `SwitchSettingCard`: 开机自启、关闭时隐藏到托盘。

---

## 4. 弹窗交互设计 (Dialogs & Pop-ups)

### 4.1 紧急消息弹窗 (Urgent Message)

- **触发条件：** 接收到 `type: urgent` 消息。
- **组件：** `MessageBox` 或 `TeachingTip`。
- **样式：** * 标题：`🚨 紧急消息`
- 内容：显示发送者与正文。
- 按钮：提供“我知道了（已读）”按钮，以及稍后提醒按钮，具体延迟多少时间可以用一个输入框或下拉框设置，默认为设置中的设置。
- **行为：** 立即置顶弹出，不打开主窗口，直到用户点击按钮。

### 4.2 课间自动弹出

- **逻辑：** 满足时间表且有未读普通消息。
- **组件：** 主窗口通过 `show()` 和 `raise_()` 呼出。
- **反馈：** 在桌面右下角显示一个 `InfoBar` (Success 类型) 提示：“课间休息，有 X 条新消息”。

---

## 5. 动效需求 (Animations)

- **列表入场：** 消息卡片产生时，带有一个轻微的从下往上滑动的淡入效果。
- **已读消除：** 点击“已读”后，卡片向右滑动并淡出。
- **状态切换：** 标题栏状态文字切换时使用淡入淡出动画。

---

## 6. 异常状态 UI 展示

- **网络断开：** 主窗口上方悬挂一个 `InfoBar` (Warning 类型) 提示：“连接服务器失败，正在尝试重连...”，且设置页的连接状态指示灯变红。
- **空状态：** 当没有消息时，在页面中心展示 `EmptyWidget`（一个淡灰色的图标和“暂时没有消息哦”的文字）。

---

