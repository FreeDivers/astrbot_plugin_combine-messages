from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Plain, Image, BaseMessageComponent
import asyncio
import uuid
import time
from astrbot.api.platform import AstrBotMessage
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.star_handler import star_handlers_registry
from astrbot.api import AstrBotConfig


# 预留图片识别接口
async def recognize_image_content(image: Image) -> str:
    # TODO: 调用实际图片识别API
    return f"[图片:{image.url or image.file or '未知'}]"


class MessageBuffer:
    def __init__(self, context=None):
        self.buffer_pool: dict[str, dict] = {}
        self.lock = asyncio.Lock()
        self.interval_time = 3
        self.initial_delay = 0.5  # 新增初始强制延迟
        self.context = context

    def get_session_id(self, event: AstrMessageEvent) -> str:
        if event.is_private_chat():
            return f"private_{event.get_sender_id()}"
        else:
            gid = getattr(event.message_obj, "group_id", "unknown")
            return f"group_{gid}_{event.get_sender_id()}"

    async def add_component(
        self, event: AstrMessageEvent, component: BaseMessageComponent
    ) -> None:
        sid = self.get_session_id(event)
        async with self.lock:
            if sid not in self.buffer_pool:
                self.buffer_pool[sid] = {
                    "components": [],
                    "timer": None,
                    "event": event,
                    "delay_task": None,
                }
            # For Plain text, we can merge with the previous one if it's also Plain text.
            if (
                isinstance(component, Plain)
                and self.buffer_pool[sid]["components"]
                and isinstance(self.buffer_pool[sid]["components"][-1], Plain)
            ):
                self.buffer_pool[sid]["components"][-1].text += f"，{component.text}"
            else:
                self.buffer_pool[sid]["components"].append(component)
            # Reset timer
            if self.buffer_pool[sid]["timer"]:
                self.buffer_pool[sid]["timer"].cancel()
                try:
                    await self.buffer_pool[sid]["timer"]
                except asyncio.CancelledError:
                    pass
            if self.buffer_pool[sid]["delay_task"]:
                self.buffer_pool[sid]["delay_task"].cancel()
                try:
                    await self.buffer_pool[sid]["delay_task"]
                except asyncio.CancelledError:
                    pass
            self.buffer_pool[sid]["delay_task"] = asyncio.create_task(
                self._wait_and_start_merge(sid)
            )
            self.buffer_pool[sid]["event"] = event

    async def _wait_and_start_merge(self, sid: str) -> None:
        await asyncio.sleep(self.initial_delay)
        async with self.lock:
            if sid in self.buffer_pool:
                if self.buffer_pool[sid]["timer"]:
                    self.buffer_pool[sid]["timer"].cancel()
                    try:
                        await self.buffer_pool[sid]["timer"]
                    except asyncio.CancelledError:
                        pass
                self.buffer_pool[sid]["timer"] = asyncio.create_task(
                    self._wait_and_merge(sid)
                )

    async def _wait_and_merge(self, sid: str) -> None:
        await asyncio.sleep(self.interval_time)
        async with self.lock:
            buf = self.buffer_pool.get(sid)
            if not buf:
                return
            components = buf.get("components", [])
            event = buf.get("event")
            if not event or not components:
                self.buffer_pool.pop(sid, None)
                return
            merged_str_parts = []
            for comp in components:
                if isinstance(comp, Plain):
                    merged_str_parts.append(comp.text.strip())
                elif isinstance(comp, Image):
                    # 调用图片识别接口
                    merged_str_parts.append(await recognize_image_content(comp))
            merged_str = " ".join(merged_str_parts)
            if not merged_str.strip():
                self.buffer_pool.pop(sid, None)
                return
            logger.info(f"合并多媒体消息: {merged_str[:50]}...")
            try:
                new_message_obj = AstrBotMessage()
                new_message_obj.type = event.message_obj.type
                new_message_obj.self_id = event.message_obj.self_id
                new_message_obj.session_id = event.message_obj.session_id
                new_message_obj.group_id = getattr(event.message_obj, "group_id", "")
                new_message_obj.sender = event.message_obj.sender
                new_message_obj.raw_message = event.message_obj.raw_message
                new_message_obj.message_str = merged_str
                new_message_obj.message = components
                new_message_obj.timestamp = int(time.time())
                original_msg_id = getattr(
                    event.message_obj, "message_id", str(uuid.uuid4())
                )
                new_message_obj.message_id = (
                    f"combined-{original_msg_id}-{int(time.time() * 1000)}"
                )
                event_args = {
                    "message_str": merged_str,
                    "message_obj": new_message_obj,
                    "platform_meta": event.platform_meta,
                    "session_id": event.session_id,
                }
                if hasattr(event, "bot"):
                    event_args["bot"] = event.bot
                new_event = type(event)(**event_args)
                new_event.is_wake = True
                if self.context:
                    self.context.get_event_queue().put_nowait(new_event)
                    logger.info("已将合并的多媒体消息推入事件队列以供LLM处理。")
                else:
                    logger.error("无法推送合并消息，因为Context丢失。")
            except Exception as e:
                logger.error(f"创建或推送合并多媒体消息事件失败: {e}", exc_info=True)
            finally:
                self.buffer_pool.pop(sid, None)

    async def shutdown(self) -> None:
        async with self.lock:
            for sid, buf in list(self.buffer_pool.items()):
                for key in ("timer", "delay_task"):
                    task = buf.get(key)
                    if task:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
            self.buffer_pool.clear()


@register("combine_messages", "合并消息", "自动合并连续消息，防止刷屏", "2.0.0")
class CombineMessagesPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.enabled = True
        self.interval_time = 3
        self.initial_delay = 0.5
        global message_buffer
        message_buffer = MessageBuffer(context)

    async def initialize(self):
        message_buffer.interval_time = self.interval_time
        message_buffer.initial_delay = getattr(self, 'initial_delay', 0.5)
        message_buffer.context = self.context
        logger.info("消息合并插件已初始化")

    async def shutdown(self):
        await self.message_buffer.shutdown()

    def get_all_command_names(self) -> set[str]:
        # ---- 安全初始化，防止未经过 __init__ 时访问报错 ----
        if not hasattr(self, "_command_names_cache"):
            self._command_names_cache = set()
            self._command_names_cache_time = 0
            self._command_names_cache_ttl = 60
        # -----------------------------------------------
        now = time.time()
        if (
            self._command_names_cache
            and now - self._command_names_cache_time < self._command_names_cache_ttl
        ):
            return self._command_names_cache
        command_names = set()
        try:
            for star in self.context.get_all_stars():
                if not getattr(star, "activated", False):
                    continue
                module_path = getattr(star, "module_path", None)
                for handler in star_handlers_registry:
                    if getattr(handler, "handler_module_path", None) != module_path:
                        continue
                    for f in getattr(handler, "event_filters", []):
                        if isinstance(f, CommandFilter):
                            command_names.add(f.command_name)
                            break
                        elif isinstance(f, CommandGroupFilter):
                            command_names.add(f.group_name)
                            break
        except Exception as e:
            logger.error(f"获取命令名失败: {e}")
        # 实时读取最新配置
        extra_commands = set(self.config.get("extra_commands", ["llm"]))
        command_names.update(extra_commands)
        self._command_names_cache = command_names
        self._command_names_cache_time = now
        return command_names

    @filter.command("combine_on")
    async def enable_combine(self, event: AstrMessageEvent):
        self.enabled = True
        logger.info("已开启消息合并功能")
        try:
            await event.send(MessageChain([Plain("已开启消息合并功能")]))
        except Exception as e:
            logger.error(f"回复消息失败: {e}")
            yield event.plain_result("已开启消息合并功能")

    @filter.command("combine_off")
    async def disable_combine(self, event: AstrMessageEvent):
        self.enabled = False
        logger.info("已关闭消息合并功能")
        try:
            await event.send(MessageChain([Plain("已关闭消息合并功能")]))
        except Exception as e:
            logger.error(f"回复消息失败: {e}")
            yield event.plain_result("已关闭消息合并功能")

    @filter.command("combine_interval")
    async def set_interval(self, event: AstrMessageEvent):
        try:
            args = event.message_str.split()
            if len(args) > 1:
                interval = float(args[1])
                interval = min(max(interval, 0.1), 10)
                self.interval_time = interval
                message_buffer.interval_time = interval
                response = f"已设置消息合并间隔为 {interval} 秒"
            else:
                response = f"当前消息合并间隔为 {self.interval_time} 秒"
                
            logger.info(response)
            try:
                await event.send(MessageChain([Plain(response)]))
            except Exception as e:
                logger.error(f"回复消息失败: {e}")
                yield event.plain_result(response)
        except Exception as e:
            error_msg = f"设置失败: {str(e)}"
            logger.error(error_msg)
            try:
                await event.send(MessageChain([Plain(error_msg)]))
            except Exception as e2:
                logger.error(f"回复消息失败: {e2}")
                yield event.plain_result(error_msg)

    @filter.command("combine_delay")
    async def set_delay(self, event: AstrMessageEvent):
        try:
            args = event.message_str.split()
            if len(args) > 1:
                delay = float(args[1])
                delay = min(max(delay, 0.1), 2)
                self.initial_delay = delay
                message_buffer.initial_delay = delay
                response = f"已设置初始强制延迟为 {delay} 秒"
            else:
                response = f"当前初始强制延迟为 {getattr(self, 'initial_delay', 0.5)} 秒"
                
            logger.info(response)
            try:
                await event.send(MessageChain([Plain(response)]))
            except Exception as e:
                logger.error(f"回复消息失败: {e}")
                yield event.plain_result(response)
        except Exception as e:
            error_msg = f"设置失败: {str(e)}"
            logger.error(error_msg)
            try:
                await event.send(MessageChain([Plain(error_msg)]))
            except Exception as e2:
                logger.error(f"回复消息失败: {e2}")
                yield event.plain_result(error_msg)

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE | filter.EventMessageType.PRIVATE_MESSAGE
    )
    async def on_message(self, event: AstrMessageEvent, ctx, *args, **kwargs):
        if (
            hasattr(event.message_obj, "message_id")
            and isinstance(event.message_obj.message_id, str)
            and event.message_obj.message_id.startswith("combined-")
        ) or not self.enabled:
            return
        
        msg_text = event.message_str.strip()
        all_commands = self.get_all_command_names()
        first_token = msg_text.split()[0] if msg_text else ""

        # 实时读取 block_prefixes
        block_prefixes = tuple(
            self.config.get("block_prefixes", ["/", "!", "！", ".", "。", "#", "%"])
        )

        if (
            msg_text.startswith(block_prefixes)
            or first_token in all_commands
            or msg_text in all_commands
            or "[SYS_PROMPT]" in msg_text
        ):
            logger.debug(f"跳过指令或特殊消息：{msg_text[:20]}...")
            return

        has_content_to_merge = False
        for comp in getattr(event.message_obj, "message", []):
            if isinstance(comp, Plain) and comp.text and comp.text.strip():
                plain_text = comp.text.strip()
                if plain_text.startswith(block_prefixes) or plain_text in all_commands:
                    logger.debug(f"跳过组件前缀或命令：{plain_text[:20]}...")
                    continue
                await self.message_buffer.add_component(event, comp)
                has_content_to_merge = True
            elif isinstance(comp, Image):
                await self.message_buffer.add_component(event, comp)
                has_content_to_merge = True

        if has_content_to_merge:
            logger.info(f"消息已缓存用于合并: {event.get_message_outline()[:30]}...")
            event.stop_event()
