# 🛠️ 家校沟通项目 - 技术说明文档 (Technical Spec)

## 1. 架构拓扑 (Architecture)

* **Nonebot2 Plugin:** 部署于服务器，作为 QQ 协议适配器。
* **Flask Server:** 核心中转站，维护 WebSocket 长连接，管理 SQLite 数据库。
* **PyQt6 Client:** 终端显示设备，通过 WebSocket 与 Server 通信。

---

## 2. 数据库设计 (Database Schema)

使用 **SQLite3**，表名：`messages`

| 字段名 | 类型 | 说明 |
| --- | --- | --- |
| `id` | INTEGER | 自增主键 |
| `group_id` | TEXT | 来源群号 (预留扩展) |
| `sender_id` | TEXT | 发送者 QQ 号 |
| `sender_name` | TEXT | 发送者群名片/昵称 |
| `content` | TEXT | 消息纯文本内容 |
| `msg_type` | TEXT | `normal` (普通) 或 `urgent` (紧急) |
| `status` | TEXT | `unread` (未读), `read` (已读), `recalled` (已撤回) |
| `resend_count` | INTEGER | 重发次数，默认为 0 |
| `timestamp` | DATETIME | 消息接收时间 |
| `resend_time` | DATETIME | 最后一次重发的时间 |

---

## 3. WebSocket 通信协议 (JSON)

全链路使用 JSON 格式。所有消息必须包含 `type` 字段。

### 3.1 Plugin -> Server (上报消息)

```json
{
  "type": "new_message",
  "auth_token": "YOUR_INTERNAL_TOKEN",
  "data": {
    "sender_name": "xxx妈妈",
    "sender_id": "123456",
    "content": "记得带雨伞",
    "msg_type": "normal",
    "timestamp": "2023-10-27 10:00:00"
  }
}

```

### 3.2 Client -> Server (已读确认)

```json
{
  "type": "read_ack",
  "short_id": "1024"
}

```

### 3.3 Server -> Plugin (回传给家长已读回执)

```json
{
  "type": "send_receipt",
  "target_qq": "123456",
  "short_id": "1024",
  "text": "[回执] 您的消息已由学生接收。"
}

```

### 3.4 状态同步 (Client Status)

* **考试模式切换：** Client 发送 `{ "type": "status_update", "mode": "exam_on/exam_off" }`。
* **心跳：** 通过WS连接状态判断，连接即为正常，client无法连接server但plugin可以连接server，则client离线（可能处于关机状态），如都无法连接server，则消息服务器server可能崩了。

---

## 4. 关键逻辑实现建议

### 4.1 短 ID 生成算法

* 在 `Server` 端实现。每次收到查询命令时：
1. 列出未读消息，赋予短id，如第一个未读消息短id为1，第二个为2，以此类推。
2. 创建内存保存短id与实际id的对应关系。
3. 5分钟内，用户可以凭短id撤回或重发未读消息，注意核实用户身份是否为发送者。
4. 5分钟后销毁列表，释放内存，短id失效。



### 4.2 时间表与 NTP 同步

* **NTP 实现：** 客户端使用 `ntplib` 库。
```python
import ntplib
def get_ntp_time():
    try:
        client = ntplib.NTPClient()
        response = client.request('ntp.aliyun.com', version=3)
        return response.tx_time  # 返回时间戳
    except:
        return time.time() # 降级使用系统时间

```


* **时间表匹配：**
* 将 `breaks` 中的 `start` 和 `end` 转换为 `datetime.time` 对象。
* 判断当前 NTP 时间是否在任何一个 `(start, end)` 闭区间内。



### 4.3 考试模式静默逻辑

* **Plugin 侧：** 维护一个全局变量 `EXAM_MODE = False`。
* 当收到 Client 的状态切换信号后更新该变量。
* `on_message` 触发时，若 `EXAM_MODE == True`，则在回复家长时附加考试模式提醒。

---

## 5. 开发环境与依赖清单

* **Python 版本：** 3.9.7
* **Nonebot 插件库：** `nonebot2`, `nonebot-adapter-onebot`
* **Server 端库：** `flask`, `flask-socketio`, `sqlalchemy`
* **Client 端库：** `PyQt6`, `PyQt6-Fluent-Widgets`, `ntplib`, `websockets`

---

## 6. 安全与防护

1. **HTML 转义：** 客户端在 `MessageCard` 渲染文本前，必须调用 `html.escape(content)`。
2. **WS 断线重连：** 客户端需实现指数避退（Exponential Backoff）重连算法，防止服务器重启时产生瞬时高并发请求。
3. **鉴权：** Server 启动时生成一个随机 UUID 作为 `INTERNAL_TOKEN`，手动填入 Plugin 和 Client 的配置文件中。
