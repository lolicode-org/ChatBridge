import html
import json
import re
from typing import Optional

import websocket
from token_bucket import MemoryStorage, Limiter

from chatbridge.common.logger import ChatBridgeLogger
from chatbridge.core.client import ChatBridgeClient
from chatbridge.core.network.protocol import ChatPayload, CommandPayload
from chatbridge.impl import utils
from chatbridge.impl.cqhttp.config import CqHttpConfig
from chatbridge.impl.mcdr.protocol import RemoteCommandResult
from chatbridge.impl.tis.protocol import StatsQueryResult, OnlineQueryResult

ConfigFile = 'ChatBridge_CQHttp.json'
cq_bot: Optional['CQBot'] = None
chatClient: Optional['CqHttpChatBridgeClient'] = None

rate_storage: Optional['MemoryStorage'] = None
qq_limiter: Optional['Limiter'] = None
mc_limiter: Optional['Limiter'] = None

CQHelpMessage = '''
!!help: 显示本条帮助信息
!!ping: pong!!
!!mc <消息>: 向 MC 中发送聊天信息 <消息>
!!online: 显示在线列表
!!stats <类别> <内容> [<-bot>]: 查询统计信息 <类别>.<内容> 的排名
'''.strip()
StatsHelpMessage = '''
!!stats <类别> <内容> [<-bot>]
添加 `-bot` 来列出 bot
例子:
!!stats used diamond_pickaxe
!!stats custom time_since_rest -bot
'''.strip()


class CQBot(websocket.WebSocketApp):
    def __init__(self, config: CqHttpConfig):
        self.config = config
        websocket.enableTrace(True)
        url = 'ws://{}:{}/'.format(self.config.ws_address, self.config.ws_port)
        if self.config.access_token is not None:
            url += '?access_token={}'.format(self.config.access_token)
        self.logger = ChatBridgeLogger('Bot', file_handler=chatClient.logger.file_handler)
        self.logger.info('Connecting to {}'.format(url))
        # noinspection PyTypeChecker
        super().__init__(url, on_message=self.on_message, on_close=self.on_close)

    def start(self):
        self.run_forever()

    def on_message(self, _, message: str):
        try:
            if chatClient is None:
                return
            data = json.loads(message)
            if data.get('post_type') == 'message' and data.get('message_type') == 'group':
                if data['anonymous'] is None and data['group_id'] == self.config.react_group_id:
                    self.logger.info('QQ chat message: {}'.format(data))
                    args = data['raw_message'].split(' ')

                    if self.config.qq_whitelist:
                        if int(data['user_id']) not in self.config.qq_list:
                            return
                    else:
                        if int(data['user_id']) in self.config.qq_list:
                            return

                    if data.get('raw_message', '').strip().startswith("#"):
                        cmd = data['raw_message'][1:].strip().split(maxsplit=2)
                        if len(cmd) > 2:
                            if int(data['user_id']) in self.config.admin:
                                if cmd[0] == "/":
                                    if chatClient.is_online():
                                        self.logger.info("Vanilla command triggered")
                                        chatClient.send_command(cmd[1], cmd[2], params={"IsQQ": True, "Type": "Vanilla"})
                                        self.logger.info(f"Sent command {cmd[2]} to client {cmd[1]}")
                                    else:
                                        self.send_text("ChatBridge 客户端离线")
                                    return
                                elif cmd[0] == "!":
                                    if chatClient.is_online():
                                        self.logger.info("MCDR command triggered")
                                        chatClient.send_command(cmd[1], cmd[2], params={"IsQQ": True, "Type": "MCDR"})
                                        self.logger.info(f"Sent command {cmd[2]} to client {cmd[1]}")
                                    else:
                                        self.send_text("ChatBridge 客户端离线")
                                    return
                            elif cmd[0].lower() in ["离线", "offline"] and \
                                (int(data['user_id']) in self.config.admin or self.config.allow_easyauth_offline_reg_for_everyone):
                                u_name = cmd[2].strip()
                                if not utils.is_valid_minecraft_username(u_name):
                                    self.send_text("非法的用户名！")
                                    return
                                if chatClient.is_online():
                                    cmd_to_send = f"auth addToForcedOffline {u_name}"
                                    self.logger.info("Easyauth Offline register triggered")
                                    chatClient.send_command(cmd[1], cmd_to_send, params={"IsQQ": True, "Type": "Vanilla"})
                                    self.logger.info(f"Sent command {cmd_to_send} to client {cmd[1]}")
                                else:
                                    self.send_text("ChatBridge 客户端离线")
                                return
                            elif cmd[0].lower() in ["白名单", "whitelist"] and \
                                (int(data['user_id']) in self.config.admin or self.config.allow_whitelist_for_everyone):
                                u_name = cmd[2].strip()
                                if not utils.is_valid_minecraft_username(u_name):
                                    self.send_text("非法的用户名！")
                                    return
                                if chatClient.is_online():
                                    cmd_to_send = f"whitelist add {u_name}"
                                    self.logger.info("Whitelist triggered")
                                    chatClient.send_command(cmd[1], cmd_to_send, params={"IsQQ": True, "Type": "Vanilla"})
                                    self.logger.info(f"Sent command {cmd_to_send} to client {cmd[1]}")
                                else:
                                    self.send_text("ChatBridge 客户端离线")
                                return
                            else:
                                self.send_text("命令执行失败：格式错误或权限不足")
                                return

                    if len(args) == 1:
                        if args[0] == '!!help':
                            self.logger.info('!!help command triggered')
                            self.send_text(CQHelpMessage)
                            return

                        elif args[0] == '!!ping':
                            self.logger.info('!!ping command triggered')
                            self.send_text('pong!!')
                            return

                        elif args[0] == '!!online':
                            self.logger.info('!!online command triggered')
                            if chatClient.is_online():
                                command = args[0]
                                client = self.config.client_to_query_online
                                self.logger.info('Sending command "{}" to client {}'.format(command, client))
                                chatClient.send_command(client, command)
                            else:
                                self.send_text('ChatBridge 客户端离线')
                            return

                    if len(args) >= 1 and args[0] == '!!stats':
                        self.logger.info('!!stats command triggered')
                        command = '!!stats rank ' + ' '.join(args[1:])
                        if len(args) == 0 or len(args) - int(command.find('-bot') != -1) != 3:
                            self.send_text(StatsHelpMessage)
                            return
                        if chatClient.is_online:
                            client = self.config.client_to_query_stats
                            self.logger.info('Sending command "{}" to client {}'.format(command, client))
                            chatClient.send_command(client, command)
                        else:
                            self.send_text('ChatBridge 客户端离线')
                        return

                    if len(args) == 3 and args[0] == '!!killbot':
                        self.logger.info('!!killbot command triggered')
                        if chatClient.is_online():
                            command = f'player {args[2]} kill'
                            self.logger.info(f'Sending command {command} to client {args[1]}')
                            chatClient.send_command(args[1], command, params={"IsQQ": True, "Type": "Vanilla"})
                        else:
                            self.send_text('ChatBridge 客户端离线')
                        return

                    if self.config.qq_to_mc_auto or (len(args) >= 2 and args[0] == '!!mc'):
                        if int(data['user_id']) not in self.config.admin and self.config.qq_limiter:
                            if not qq_limiter.consume('qq'):
                                self.logger.warning('Message not forwarded due to rate limiting')
                                return
                        self.logger.info('Message forward triggered')
                        sender = data['sender']['card']
                        if len(sender) == 0:
                            sender = data['sender']['nickname']
                        text = html.unescape(data['raw_message']) if self.config.qq_to_mc_auto \
                            else html.unescape(data['raw_message'].split(' ', 1)[1])
                        text = re.sub(
                            r"\[CQ:(\w+)(,(\w+)=(.*?))*]",
                            "[不支持的消息格式]", text
                        )

                        if 0 < self.config.qq_max_length < len(text):
                            self.logger.warning('Message not forwarded because it exceeded the length limit')
                            return

                        chatClient.send_chat(text, sender)

        except:
            self.logger.exception('Error in on_message()')

    def on_close(self, *args):
        self.logger.info("Close connection")

    def _send_text(self, text):
        data = {
            "action": "send_group_msg",
            "params": {
                "group_id": self.config.react_group_id,
                "message": text
            }
        }
        self.send(json.dumps(data))

    def send_text(self, text):
        msg = ''
        length = 0
        lines = text.rstrip().splitlines(keepends=True)
        for i in range(len(lines)):
            msg += lines[i]
            length += len(lines[i])
            if i == len(lines) - 1 or length + len(lines[i + 1]) > 500:
                self._send_text(msg)
                msg = ''
                length = 0

    def send_message(self, sender: str, message: str):
        self.send_text('[{}] {}'.format(sender, message))


class CqHttpChatBridgeClient(ChatBridgeClient):
    config: Optional['CqHttpConfig'] = None

    @classmethod
    def create(cls, config: CqHttpConfig):
        self = cls(config.aes_key, config.client_info, server_address=config.server_address)
        self.config = config
        return self

    def on_chat(self, sender: str, payload: ChatPayload):
        global cq_bot
        if cq_bot is None:
            return
        try:
            if self.config.mc_whitelist:
                if payload.author not in self.config.mc_list:
                    return
            else:
                if payload.author in self.config.mc_list:
                    return

            if self.config.mc_to_qq_auto and not payload.message.strip().startswith('!!'):
                if (not self.config.forward_join_message) \
                        and (re.match(r'.+ (joined|left) .+', payload.message.strip())):
                    return
                if self.config.mc_limiter and not mc_limiter.consume('mc'):
                    self.logger.warning('Message not forwarded due to rate limiting')
                    return
                self.logger.info('Message forward triggered')

                text = payload.formatted_str()
                if 0 < self.config.mc_max_length < len(text):
                    self.logger.warning('Message not forwarded because it exceeded the length limit')
                    return
                cq_bot.send_message(sender, text)
            else:
                try:
                    prefix, message = payload.message.split(' ', 1)
                except:
                    pass
                else:
                    if prefix == '!!qq':
                        if self.config.mc_limiter and not mc_limiter.consume('mc'):
                            self.logger.warning('Message not forwarded due to rate limiting')
                            return
                        self.logger.info('Triggered command, sending message {} to qq'.format(payload.formatted_str()))
                        payload.message = message

                        text = payload.formatted_str()
                        if 0 < self.config.mc_max_length < len(text):
                            self.logger.warning('Message not forwarded because it exceeded the length limit')
                            return
                        cq_bot.send_message(sender, text)
        except:
            self.logger.exception('Error in on_message()')

    def on_command(self, sender: str, payload: CommandPayload):
        if not payload.responded:
            return
        if payload.command.startswith('!!stats '):
            result = StatsQueryResult.deserialize(payload.result)
            if result.success:
                messages = ['====== {} ======'.format(result.stats_name)]
                messages.extend(result.data)
                messages.append('总数：{}'.format(result.total))
                cq_bot.send_text('\n'.join(messages))
            elif result.error_code == 1:
                cq_bot.send_text('统计信息未找到')
            elif result.error_code == 2:
                cq_bot.send_text('StatsHelper 插件未加载')
        elif payload.command == '!!online':
            result = OnlineQueryResult.deserialize(payload.result)
            cq_bot.send_text('====== 玩家列表 ======\n{}'.format('\n'.join(result.data)))
        elif payload.params.get("IsQQ"):
            result = RemoteCommandResult.deserialize(payload.result)
            cq_bot.send_text("命令已执行" if result.success else "Minecraft服务器未在运行，请稍后再试")


def main():
    global chatClient, cq_bot, rate_storage, qq_limiter, mc_limiter
    config = utils.load_config(ConfigFile, CqHttpConfig)
    rate_storage = MemoryStorage()
    qq_limiter = Limiter(rate=config.qq_limiter_rate, capacity=config.qq_limiter_capacity, storage=rate_storage)
    mc_limiter = Limiter(rate=config.mc_limiter_rate, capacity=config.mc_limiter_capacity, storage=rate_storage)
    chatClient = CqHttpChatBridgeClient.create(config)
    utils.start_guardian(chatClient)
    print('Starting CQ Bot')
    cq_bot = CQBot(config)
    cq_bot.start()
    print('Bye~')


if __name__ == '__main__':
    main()
