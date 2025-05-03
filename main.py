from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Plain
import asyncio
import time
import hashlib
from collections import OrderedDict
from dataclasses import dataclass, field


@dataclass
class CacheMessages:
    message: AstrMessageEvent
    cache_determination: asyncio.Event = field(default_factory=asyncio.Event)  # 判断缓冲是否产生结果
    result: str = "U"  # U: 未处理, T: 处理成功, F: 处理失败


class MessageBuffer:
    def __init__(self):
        self.buffer_pool = {}  # 缓冲池，存储用户的消息
        self.lock = asyncio.Lock()
        self.interval_time = 3  # 默认等待时间（秒）

    @staticmethod
    def get_person_id_(platform: str, user_id: str, group_id: str = None):
        """获取唯一id"""
        if not group_id:
            group_id = "私聊"
        key = f"{platform}_{user_id}_{group_id}"
        return hashlib.md5(key.encode()).hexdigest()

    async def start_caching_messages(self, event: AstrMessageEvent):
        """添加消息，启动缓冲"""
        # 获取发送者信息
        platform = event.platform_meta.name
        user_id = event.get_sender_id()
        group_id = None
        if not event.is_private_chat():
            group_id = event.message_obj.group_id
        
        person_id_ = self.get_person_id_(platform, user_id, group_id)
        message_id = event.message_obj.message_id

        async with self.lock:
            if person_id_ not in self.buffer_pool:
                self.buffer_pool[person_id_] = OrderedDict()

            # 标记该用户之前的未处理消息
            for cache_msg in self.buffer_pool[person_id_].values():
                if cache_msg.result == "U":
                    cache_msg.result = "F"
                    cache_msg.cache_determination.set()
                    logger.debug(f"被新消息覆盖信息id: {cache_msg.message.message_obj.message_id}")

            # 添加新消息
            self.buffer_pool[person_id_][message_id] = CacheMessages(message=event)

        # 启动缓冲计时器
        asyncio.create_task(self._debounce_processor(person_id_, message_id))

    async def _debounce_processor(self, person_id_: str, message_id: str):
        """等待一段时间无新消息后处理"""
        await asyncio.sleep(self.interval_time)

        async with self.lock:
            if person_id_ not in self.buffer_pool or message_id not in self.buffer_pool[person_id_]:
                logger.debug(f"消息已被清理，msgid: {message_id}")
                return

            cache_msg = self.buffer_pool[person_id_][message_id]
            if cache_msg.result == "U":
                cache_msg.result = "T"
                cache_msg.cache_determination.set()

    async def query_buffer_result(self, event: AstrMessageEvent) -> tuple:
        """查询缓冲结果，并清理
        
        Returns:
            (bool, str): (是否成功, 合并后的文本)
        """
        platform = event.platform_meta.name
        user_id = event.get_sender_id()
        group_id = None
        if not event.is_private_chat():
            group_id = event.message_obj.group_id
            
        person_id_ = self.get_person_id_(platform, user_id, group_id)
        message_id = event.message_obj.message_id

        async with self.lock:
            user_msgs = self.buffer_pool.get(person_id_, {})
            cache_msg = user_msgs.get(message_id)

            if not cache_msg:
                logger.debug(f"查询异常，消息不存在，msgid: {message_id}")
                return False, ""  # 消息不存在或已清理

        try:
            await asyncio.wait_for(cache_msg.cache_determination.wait(), timeout=10)
            result = cache_msg.result == "T"

            if result:
                async with self.lock:  # 再次加锁
                    # 清理所有早于当前消息的已处理消息，收集所有早于当前消息的F消息的文本
                    keep_msgs = OrderedDict()  # 用于存放 T 消息之后的消息
                    collected_texts = []  # 用于收集 T 消息及之前 F 消息的文本
                    process_target_found = False

                    # 遍历当前用户的所有缓冲消息
                    for msg_id, cache_msg in self.buffer_pool[person_id_].items():
                        # 如果找到了目标处理消息 (T 状态)
                        if msg_id == message_id:
                            process_target_found = True
                            # 收集这条 T 消息的文本
                            if cache_msg.message.message_str:
                                collected_texts.append(cache_msg.message.message_str)

                        # 如果已经找到了目标 T 消息，之后的消息需要保留
                        elif process_target_found:
                            keep_msgs[msg_id] = cache_msg

                        # 如果还没找到目标 T 消息，说明是之前的消息 (F 或 U)
                        else:
                            if cache_msg.result == "F":
                                # 阻止这些被合并的消息继续处理
                                if not cache_msg.message._has_send_oper:  # 检查是否已经被处理过
                                    cache_msg.message.stop_event()  # 停止消息继续传递
                                # 收集这条 F 消息的文本
                                if cache_msg.message.message_str:
                                    collected_texts.append(cache_msg.message.message_str)
                            elif cache_msg.result == "U":
                                # 记录异常情况
                                logger.warning(
                                    f"异常状态：在目标 T 消息 {message_id} 之前发现未处理的 U 消息 {cache_msg.message.message_obj.message_id}"
                                )
                                # 也收集其文本
                                if cache_msg.message.message_str:
                                    collected_texts.append(cache_msg.message.message_str)

                    # 更新缓冲池，只保留 T 消息之后的消息
                    self.buffer_pool[person_id_] = keep_msgs

                    # 生成合并后的文本
                    merged_text = ""
                    if collected_texts:
                        # 使用 OrderedDict 去重，同时保留原始顺序
                        unique_texts = list(OrderedDict.fromkeys(collected_texts))
                        
                        # 只有在收集到多于一条消息时才合并
                        if len(unique_texts) > 1:
                            merged_text = "，".join(unique_texts)
                            logger.debug(f"合并了 {len(unique_texts)} 条消息的文本内容")
                        else:
                            merged_text = unique_texts[0]

                    return result, merged_text
            return result, ""
        except asyncio.TimeoutError:
            logger.debug(f"查询超时消息id： {message_id}")
            return False, ""


# 创建全局消息缓冲器实例
message_buffer = MessageBuffer()


@register("combine_messages", "AI Specialist", "自动合并连续消息，防止刷屏", "1.0.0")
class CombineMessagesPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.enabled = True
        self.interval_time = 3  # 默认等待时间（秒）

    async def initialize(self):
        """初始化插件"""
        logger.info("消息合并插件已初始化")
        message_buffer.interval_time = self.interval_time
    
    @filter.command("combine_on")
    async def enable_combine(self, event: AstrMessageEvent):
        """开启消息合并功能"""
        self.enabled = True
        yield event.plain_result("已开启消息合并功能")
    
    @filter.command("combine_off")
    async def disable_combine(self, event: AstrMessageEvent):
        """关闭消息合并功能"""
        self.enabled = False
        yield event.plain_result("已关闭消息合并功能")
    
    @filter.command("combine_interval")
    async def set_interval(self, event: AstrMessageEvent):
        """设置消息合并的时间间隔（秒）"""
        try:
            args = event.message_str.split()
            if len(args) > 1:
                interval = float(args[1])
                if interval < 0.5:
                    interval = 0.5
                elif interval > 10:
                    interval = 10
                
                self.interval_time = interval
                message_buffer.interval_time = interval
                yield event.plain_result(f"已设置消息合并间隔为 {interval} 秒")
            else:
                yield event.plain_result(f"当前消息合并间隔为 {self.interval_time} 秒")
        except Exception as e:
            yield event.plain_result(f"设置失败: {str(e)}")
    
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_message(self, event: AstrMessageEvent):
        """处理所有消息"""
        if not self.enabled:
            return
        
        # 跳过命令消息
        if event.message_str.startswith("/"):
            return
            
        # 缓存消息
        await message_buffer.start_caching_messages(event)
        
        # 查询是否应该合并
        result, merged_text = await message_buffer.query_buffer_result(event)
        
        if result and merged_text and len(merged_text) > 0:
            # 这是一条被正确处理的T消息，如果需要合并，则修改内容
            if merged_text != event.message_str:
                # 修改原始消息内容，而不是直接回复
                event.message_str = merged_text
                # 修改消息对象中的文本
                event.message_obj.message_str = merged_text
                # 更新消息组件
                event.message_obj.message = [Plain(merged_text)]
                logger.debug(f"已合并消息: {merged_text}")
        elif not result:
            # 如果不是T消息，那可能是F消息(被合并)或U消息(等待处理)
            # 为了安全起见，我们通过缓冲区检查一下此消息的状态
            platform = event.platform_meta.name
            user_id = event.get_sender_id()
            group_id = None
            if not event.is_private_chat():
                group_id = event.message_obj.group_id
                
            person_id_ = message_buffer.get_person_id_(platform, user_id, group_id)
            message_id = event.message_obj.message_id
            
            async with message_buffer.lock:
                user_msgs = message_buffer.buffer_pool.get(person_id_, {})
                cache_msg = user_msgs.get(message_id)
                
                if cache_msg and cache_msg.result == "F":
                    # 这是一条被合并的消息，应该停止处理
                    event.stop_event()
                    logger.debug(f"阻止了被合并的消息继续处理: {event.message_str}")
    
    @filter.command("combine_test")
    async def test_combine(self, event: AstrMessageEvent):
        """测试消息合并功能"""
        yield event.plain_result("消息合并插件测试成功")
        
    async def terminate(self):
        """插件销毁方法"""
        logger.info("消息合并插件已销毁")

    # 添加一个更早拦截消息的函数，优先处理被合并的消息
    @filter.on_astrbot_loaded()
    async def register_hook(self):
        """注册Hook，在消息处理流程的早期阶段拦截被合并的消息"""
        logger.info("消息合并插件注册早期消息拦截Hook")
        
        # 定义一个早期拦截函数
        async def early_intercept(event: AstrMessageEvent):
            if not self.enabled or event.message_str.startswith("/"):
                return True  # 继续处理
                
            # 检查是否是被合并的消息
            platform = event.platform_meta.name
            user_id = event.get_sender_id()
            group_id = None
            if not event.is_private_chat():
                group_id = event.message_obj.group_id
                
            person_id_ = message_buffer.get_person_id_(platform, user_id, group_id)
            message_id = event.message_obj.message_id
            
            async with message_buffer.lock:
                user_msgs = message_buffer.buffer_pool.get(person_id_, {})
                cache_msg = user_msgs.get(message_id)
                
                if cache_msg and cache_msg.result == "F":
                    # 这是一条被合并的消息，立即拦截
                    logger.debug(f"早期拦截被合并的消息: {event.message_str}")
                    return False  # 停止处理
            
            return True  # 继续处理
        
        # 注册早期拦截函数
        if hasattr(self.context, "register_early_intercept"):
            self.context.register_early_intercept(early_intercept)
        else:
            logger.warning("当前AstrBot版本不支持早期消息拦截，合并消息可能无法完全生效")
