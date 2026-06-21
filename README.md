<div align="center">

# <image src="README_source/icon.png" height="28" width="28"/> ClassBridge
  <div style="margin-top: -16px;">
    <h3 style="color: #666666; font-weight: 400; font-size: 1.25em; margin-bottom: 20px;">教室 QQ 消息投屏系统</h3>
    <p style="margin-bottom: 25px;">
        <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License: MIT"></a>
        &nbsp;
        <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.9.7%2B-blue?logo=python&logoColor=white" alt="Python 3.9.7+"></a>
        &nbsp;
        <img src="https://img.shields.io/badge/Windows_10-22H2-0078D6?logo=windows&logoColor=white" alt="Windows 10 22H2">
        &nbsp;
        <img src="https://img.shields.io/badge/Windows_Server-2022-0078D6?logo=windows&logoColor=white" alt="Windows Server 2022">
    </p>
  </div>
</div>

---

家长在 QQ 群里发消息，实时转发到教室大屏幕上。支持消息优先级（普通 / 紧急）、已读回执、撤回、重发、考试静默模式、课间自动弹窗、管理员（科任老师）私聊。

---


## 架构

```
家长 QQ 群 → NapCat/NoneBot → WebSocket Server → 教室电脑客户端
                 ↑                                     │
                 └──────── 已读回执 ←───────────────────┘
```

三组件各自独立部署：

| 组件         | 目录                      | 运行位置             |
|------------|-------------------------|------------------|
| **Server** | `server/`               | 服务器（公网可达）        |
| **Client** | `client/`               | 教室电脑（Windows）    |
| **Plugin** | `Nonebot/kgGao29Robot/` | 服务器（与 NapCat 同机） |

## 快速开始

### 推荐环境（即开发环境，其它环境我没测过不知道）

- Python 3.9
- Windows 10 22H2（Client 端）、 Windows Server 2022（Nonebot及Server端）

---

### 1. Server（消息中转服务器）

```bash
# 进入项目目录
cd kegao_qq_bot_codex

# 安装依赖
pip install websockets==11.0.3 sqlalchemy

# 复制并编辑配置文件
cp configs/server.example.toml configs/server.toml
# 编辑 configs/server.toml：
#   - 修改 internal_token 为一个随机字符串
#   - host 保持 127.0.0.1（仅本机监听）
#   - 如需直连可改为 0.0.0.0，但仅建议内网使用

# 启动
python -m server
```

服务启动后监听 `ws://127.0.0.1:8765`，提供两个 WebSocket 端点：
- `/ws/client` — 教室客户端
- `/ws/plugin` — QQ 机器人插件

**注意：** `server` 应当运行于具有公网IP的服务器上，且服务器不可停机，否则将会导致消息丢失

---

### 2. Client（教室桌面客户端）

```bash
# 安装依赖
pip install -r client/requirements.txt

# 复制并编辑配置文件
cp configs/client.example.toml configs/client.toml
# 编辑 configs/client.toml：
#   - internal_token：与 server.toml 保持一致
#   - server_ws_url：指向 server 的公网地址，如 ws://你的服务器IP:8765/ws/client
#   - 按需修改 [client] 和 [schedule] 下的设置项

# 启动
python -m client
```

客户端启动后：
- 最小化到系统托盘（关闭窗口不会退出）
- 课间时间（按 `[schedule]` 配置）自动弹出未读消息
- 紧急消息弹出模态对话框强制提醒

**考试模式**：在设置页勾选后，标题栏显示 🌙，暂停自动弹窗。

**访问验证**：修改服务器地址等敏感设置需输入验证密码。验证密码和验证服务地址在配置文件 `[challenge]` 段中设置。 

验证服务是一个Flask编写的服务器，以下是一个简单的示例：
```python
from flask import Flask, request, jsonify, render_template
import random

QUESTION_POOL = [
    {"id": 1, "q": "问题1", "a": "答案1"},
    {"id": 2, "q": "问题2", "a": "答案2"},
    {"id": 3, "q": "问题3", "a": "答案3"}
]

app = Flask(__name__)

def pick_random_question():
    chosen = random.choice(QUESTION_POOL)
    return {"id": chosen['id'], "q": chosen['q']}
    
# 通过 /challenge 获取问题id及题目
@app.route('/challenge', methods=['GET'])
def challenge():
    q = pick_random_question()
    return jsonify({"status": "success", "id": q['id'], "question": q['q']}), 200

# 通过 /verify 验证答案是否正确
@app.route('/verify', methods=['POST'])
def verify():
    data = request.get_json(force=True, silent=True) or {}
    qid = data.get('id')
    answer = data.get('answer', '')

    if qid is None or not isinstance(qid, int):
        return jsonify({"status": "error", "message": "缺少问题 id"}), 400

    question = next((q for q in QUESTION_POOL if q['id'] == qid), None)
    if not question:
        return jsonify({"status": "error", "message": "无效的问题 id"}), 400

    if str(answer).strip().lower() == str(question['a']).strip().lower():
        return jsonify({
            "status": "success",
            "message": "验证通过"
        }), 200
    else:
        return jsonify({"status": "error", "message": "答案错误"}), 403
    
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=6666)  # 此处端口号应与 client.toml 中的 challenge_url 及 verify_url 一致

```

不需要验证服务的话把相关代码删了也行，这个功能并不重要

release 中的 client 不包含验证功能。验证功能的初衷是防止路过的同学好奇去乱改服务器地址，增加维护成本

---

### 3. Plugin（NoneBot QQ 机器人）

#### 3.1 安装 NapCat

前往 [NapCat 官方仓库](https://github.com/NapNeko/NapCatQQ) 下载对应环境的一键包并按照指引安装。

#### 3.2 配置 NapCat

有关自动登录、密码、插件等按需配置即可，此处主要强调网络配置。请在 NapCat 主界面中依次点击：`网络配置`-`新建`-`Websocket客户端`，并参考 `Nonebot/appsettings.json` 中定义的连接方式及端口号：

```json
{
    "Implementations": [
        {
            "Type": "ReverseWebSocket",
            "Host": "127.0.0.1",
            "Port": 8080,
            "Suffix": "/onebot/v11/ws",
            "ReconnectInterval": 5000,
            "HeartBeatInterval": 5000,
            "AccessToken": ""
        }
    ]
}
```

如无特殊需求，与本项目保持一致即可，则可按下图所示进行配置：

![napcat_example.png](README_source/napcat_example.png)

**（启用按钮一定要打开！！！）**

#### 3.3 安装并配置 NoneBot

```bash
cd Nonebot/kgGao29Robot

# 创建虚拟环境
python -m venv .venv
.venv\Scripts\activate

# 安装框架
pip install nonebot2 nonebot-adapter-onebot websockets

# 复制并编辑配置文件
cp ../../configs/plugin.example.toml ../../configs/plugin.toml
# 编辑 configs/plugin.toml：
#   - internal_token：与 server.toml 保持一致
#   - server_ws_url：指向 server，如 ws://127.0.0.1:8765/ws/plugin
#   - class_group_ids：监听的 QQ 群号列表
#   - admin_users：管理员的 QQ 号列表

# 启动
nb run
```

---

### 部署拓扑建议

```
┌───────────────── 服务器 ───────────────────┐
│                                            │
│  NapCat ←── QQ 协议 ──→ QQ 服务器           │
│    │                                       │
│    └── ReverseWS → NoneBot 插件            │
│                       │                    │
│                       ↓ /ws/plugin         │
│                   Server (:8765)           │
│                       ↑                    │
│                   /ws/client               │
└───────────────────────┼────────────────────┘
                        │
      教室电脑 Client ────→ ws://服务器IP:8765/ws/client
```

建议用 Nginx 反向代理 WebSocket 端口并配置 SSL。

---

### 服务启动顺序建议
1. 启动 server （在项目根目录运行 python -m server）

2. 启动 NoneBot （进入机器人文件夹并运行 nb run）

3. 启动 NapCat （napcat.quick.bat）
   
<details>
  <summary>为什么是这个顺序</summary>
  Napcat 一启动就会疯狂重连 NoneBot，NoneBot 一启动就会疯狂重连 server

  我看着重连信息刷屏感觉很烦，于是便有了上面的顺序
</details>

---

## 家长使用说明（给群里的使用指引）

在班级 QQ 群 **@机器人** 发送消息：

```
@机器人 快递放东门了
```

可用指令：

| 指令            | 说明           |
|---------------|--------------|
| `/帮助`         | 查看帮助信息       |
| `/查询` 或 `/cx` | 查看已发送消息的阅读状态 |
| `/撤回 编号`      | 撤回未读消息       |
| `/重发 编号`      | 重发未读消息       |

---

## 配置说明

详细配置项见各 `.example.toml` 模板文件。

**重要**：三个配置文件中的 `internal_token` 必须一致，这是组件间认证凭据。

---

## 关于本项目的开发幕后
如果翻一下源代码就会发现：这代码 AI 味咋这么浓

没错，这个项目就是用 AI 写的，而 PRDs 中的那三个文件就是 vibe coding 模型参照的东西

这三个 md 文件当然也不是我写的。在立项时，我跟 Gemini 说了下我对项目的初步构想（详见 [聊天记录](README_source/Gemini_Conversation/chat.md)），跟它聊了一会后就让它生成了 PRDs，包括[产品需求文档](PRDs/PRD.md)、[UI 交互文档](PRDs/UI.md)和[技术架构文档](PRDs/Technical.md)

最开始是用 Cursor 写的，后面换成了 VSC + Codex，写完主体功能，开始完善一些细枝末节的东西时，决定换到 DeepSeek v4（性价比实在是太高了）

其实这个项目的想法在24年刚上高中的时候就有了，我们每个班都有个通知号，加了家长群，所以家长们就可以通过家长群给我们发消息，也可以直接私聊通知号。但是偶尔会有看漏消息的情况，所以我就想着做个机器人，有啥消息就弹窗，恰好开学第一周挂了个台风，连着周末有三天的台风假，于是就开始琢磨这个项目

最开始的想法是用 QQ 官方的机器人，感觉会比较靠谱一些，也不会那么容易被封掉（相比于第三方）。然后就被 SDK 卡了半天。官方机器人的 SDK 写的真的就是一坨大便，给的样例甚至本身就有错误，跑不起来。最开始我还以为是我的问题，文档反复研读还是没搞懂，后面看了下 issue 发现这并不是我的问题

![fuck_official_qq_bot_PySDK](README_source/fuck_official_qq_bot_PySDK.png)

还有下面这个 issue，xswl

![fuck_official_qq_bot_PySDK_v2](README_source/fuck_official_qq_bot_PySDK_2.png)

（对了，这个 issue 好像真被 "close" 了，这个截图是之前截的，刚刚去找没找到）

于是项目搁置，直到去年（2025年）九月，开始研究第三方机器人。最初的方案是 Nonebot（机器人） + Lagrange（QQ） ，写了个 plugin ，挂在服务器上跑。上了一周学回来发现机器人掉线了，查了一下原来是签名服务挂了。于是又去加 Telegram 群获取最新的签名服务器，又能用了

好景不长，某天放学回来发现又挂了，打开 Telegram 发现群被解散了，网上倒是有个第三方自建的签名服务，试了一下也能用，但是也崩过，加之有数据泄露的风险，于是放弃 Lagrange 这条路

那段时间好像查的很严，相关项目很多要么 archived 了，要么跑路了（也可能是进去了）。总之，签名这条路是走不通了，于是转向依赖 QQ 客户端的 Napcat，目前为止还挺稳定的

机器人端的开发告一段落，接下来开始搞中转服务器的开发，也挺简单。然后就是客户端的开发了，初版是用 Pyqt5 写的，写完后觉得有点丑，但是写都写了，将就用吧。项目至此开发就已经结束了，可以投入使用了

然后拖延症犯了，一直都没有跟班主任说这事...

今年（2026年）三月，心血来潮想试试 vibe coding，然后就如同开发幕后前半段写的那样，让 Gemini 写了 PRDs，然后交给模型开发，不赘述了

目前的计划是，从今天（26.6.21）开始试运行，一直到学期结束（26.7.10），然后根据试运行结果完善程序，九月份开学就可以正式运行啦

---

## 许可证

本项目采用 混合协议 开源，因此使用本项目时，你需要注意以下几点：
1. 第三方库代码或修改部分遵循其原始开源许可.
2. 项目其余逻辑代码采用[本仓库开源许可（MIT）](LICENSE).
