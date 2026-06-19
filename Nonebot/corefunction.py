from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata
from nonebot.rule import to_me

__plugin_meta__ = PluginMetadata(
    name="corefunction",
    description="处理消息",
    usage="/普通消息 or /紧急消息 消息内容",
    type="application",
    homepage="https://github.com/nonebot/nonebot2/blob/master/nonebot/plugins/echo.py",
    config=None,
    supported_adapters=None,
)

common_message = on_command("普通消息", to_me())


@common_message.handle()
async def handle_echo(message: Message = CommandArg()):
    if any((not seg.is_text()) or str(seg) for seg in message):
        await common_message.send(message=message)
