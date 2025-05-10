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
    creation_time: float = field(default_factory=time.time)  # 创建时间戳


class MessageBuffer:
    def __init__(self):
        self.buffer_pool = {}  # 缓冲池，存储用户的消息
        self.lock = asyncio.Lock()
        self.interval_time = 3  # 默认等待时间（秒）
        self.initial_delay = 0.5  # 初始强制延迟（秒）
        self.message_process_tasks = {}  # 存储消息处理任务
        self.last_message_time = {}  # 记录用户最后一条消息的时间

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
        
        # 兼容性检查：如果message_obj没有message_id属性，则生成一个唯一id
        try:
            message_id = event.message_obj.message_id
        except AttributeError:
            # 使用时间戳和用户id生成唯一标识
            message_id = f"{platform}_{user_id}_{int(time.time() * 1000)}"
            logger.debug(f"未找到message_id，生成临时id: {message_id}")

        # 更新用户最后消息时间
        self.last_message_time[person_id_] = time.time()

        async with self.lock:
            if person_id_ not in self.buffer_pool:
                self.buffer_pool[person_id_] = OrderedDict()

            # 标记该用户之前的未处理消息
            for msg_id, cache_msg in list(self.buffer_pool[person_id_].items()):
                if cache_msg.result == "U":
                    cache_msg.result = "F"
                    cache_msg.cache_determination.set()
                    try:
                        msg_id = cache_msg.message.message_obj.message_id
                        logger.debug(f"被新消息覆盖信息id: {msg_id}")
                    except AttributeError:
                        logger.debug("被新消息覆盖信息(无id)")

                    # 如果有正在进行的处理任务，取消它
                    if (person_id_, msg_id) in self.message_process_tasks:
                        task = self.message_process_tasks.pop((person_id_, msg_id))
                        if not task.done():
                            task.cancel()
                            logger.debug(f"取消了消息 {msg_id} 的处理任务")

            # 添加新消息
            self.buffer_pool[person_id_][message_id] = CacheMessages(message=event)

        # 启动新的缓冲计时器任务并保存引用
        task = asyncio.create_task(self._debounce_processor(person_id_, message_id))
        self.message_process_tasks[(person_id_, message_id)] = task
        
        # 确保此消息在执行前被标记已发送，防止处理中取消后重复处理
        # 兼容性检查：有些平台适配器可能没有message属性
        try:
            if hasattr(event, "message"):
                setattr(event.message, "_has_being_processed", True)
            else:
                # 直接在event对象上设置标记
                setattr(event, "_has_being_processed", True)
        except Exception as e:
            logger.debug(f"设置消息处理标记失败: {str(e)}")
        
        # 防止该消息被太快处理
        if hasattr(event, "message_obj"):
            if not getattr(event.message_obj, "_delayed", False):
                setattr(event.message_obj, "_delayed", True)
                # 添加初始延迟，使所有消息都至少等待一小段时间
                # 这给了后续消息合并的机会
                await asyncio.sleep(self.initial_delay)

    async def _debounce_processor(self, person_id_: str, message_id: str):
        """等待一段时间无新消息后处理"""
        try:
            # 先等待基本间隔时间
            await asyncio.sleep(self.interval_time)
            
            # 再检查最后消息时间，如果间隔太短，继续等待
            current_time = time.time()
            last_msg_time = self.last_message_time.get(person_id_, 0)
            time_since_last_msg = current_time - last_msg_time
            
            if time_since_last_msg < self.interval_time:
                # 如果最后一条消息时间距离现在小于合并间隔，再等待一小段时间
                additional_wait = self.interval_time - time_since_last_msg
                logger.debug(f"检测到近期有新消息，额外等待 {additional_wait:.2f} 秒")
                await asyncio.sleep(additional_wait)
            
            async with self.lock:
                if person_id_ not in self.buffer_pool or message_id not in self.buffer_pool[person_id_]:
                    logger.debug(f"消息已被清理，msgid: {message_id}")
                    return

                cache_msg = self.buffer_pool[person_id_][message_id]
                if cache_msg.result == "U":
                    cache_msg.result = "T"
                    cache_msg.cache_determination.set()
                    logger.debug(f"消息 {message_id} 被标记为 T（处理）")
                    
            # 清理任务引用
            if (person_id_, message_id) in self.message_process_tasks:
                self.message_process_tasks.pop((person_id_, message_id))
                
        except asyncio.CancelledError:
            logger.debug(f"消息 {message_id} 的处理任务被取消")
            raise
        except Exception as e:
            logger.error(f"处理消息 {message_id} 时出错: {str(e)}")

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
        
        # 兼容性检查：如果message_obj没有message_id属性，则生成一个唯一id
        try:
            message_id = event.message_obj.message_id
        except AttributeError:
            # 使用时间戳和用户id生成唯一标识
            message_id = f"{platform}_{user_id}_{int(time.time() * 1000)}"
            logger.debug(f"未找到message_id，使用临时id: {message_id}")

        async with self.lock:
            user_msgs = self.buffer_pool.get(person_id_, {})
            cache_msg = user_msgs.get(message_id)

            if not cache_msg:
                logger.debug(f"查询异常，消息不存在，msgid: {message_id}")
                return False, ""  # 消息不存在或已清理

        try:
            # 等待消息标记完成，最多等待10秒
            start_time = time.time()
            await asyncio.wait_for(cache_msg.cache_determination.wait(), timeout=10)
            elapsed = time.time() - start_time
            logger.debug(f"消息 {message_id} 等待结果用时: {elapsed:.3f}秒, 结果: {cache_msg.result}")
            
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
                                try:
                                    if not getattr(cache_msg.message, "_has_send_oper", False):  # 检查是否已经被处理过
                                        try:
                                            cache_msg.message.stop_event()  # 停止消息继续传递
                                            # 设置标记，防止重复处理
                                            setattr(cache_msg.message, "_has_send_oper", True)
                                        except Exception as e:
                                            logger.warning(f"停止消息事件失败: {str(e)}")
                                except AttributeError:
                                    # 兼容性处理
                                    logger.debug("消息对象缺少属性，无法设置标记")
                                # 收集这条 F 消息的文本
                                if cache_msg.message.message_str:
                                    collected_texts.append(cache_msg.message.message_str)
                            elif cache_msg.result == "U":
                                # 记录异常情况
                                logger.warning(
                                    f"异常状态：在目标 T 消息 {message_id} 之前发现未处理的 U 消息"
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
                            logger.debug(f"合并了 {len(unique_texts)} 条消息的文本内容: {merged_text[:50]}...")
                        else:
                            merged_text = unique_texts[0]
                            logger.debug(f"只有一条消息，不需要合并: {merged_text[:50]}...")

                    return result, merged_text
            return result, ""
        except asyncio.TimeoutError:
            logger.debug(f"查询超时消息id： {message_id}")
            return False, ""
        except Exception as e:
            logger.error(f"处理消息合并结果时出错: {str(e)}")
            return False, ""


# 创建全局消息缓冲器实例
message_buffer = MessageBuffer()


@register("combine_messages", "AI Specialist", "自动合并连续消息，防止刷屏", "1.1.0")
class CombineMessagesPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.enabled = True
        self.interval_time = 3  # 默认等待时间（秒）
        self.initial_delay = 0.5  # 初始强制延迟（秒）

    async def initialize(self):
        """初始化插件"""
        logger.info("消息合并插件已初始化")
        # 设置消息缓冲器的参数
        message_buffer.interval_time = self.interval_time
        message_buffer.initial_delay = self.initial_delay
        logger.info(f"设置合并间隔: {self.interval_time}秒, 初始延迟: {self.initial_delay}秒")
    
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
                yield event.plain_result(f"当前消息合并间隔为 {self.interval_time} 秒，初始延迟为 {self.initial_delay} 秒")
        except Exception as e:
            yield event.plain_result(f"设置失败: {str(e)}")
    
    @filter.command("combine_delay")
    async def set_initial_delay(self, event: AstrMessageEvent):
        """设置初始强制延迟时间（秒）"""
        try:
            args = event.message_str.split()
            if len(args) > 1:
                delay = float(args[1])
                if delay < 0.1:
                    delay = 0.1
                elif delay > 2:
                    delay = 2
                
                self.initial_delay = delay
                message_buffer.initial_delay = delay
                yield event.plain_result(f"已设置初始强制延迟为 {delay} 秒")
            else:
                yield event.plain_result(f"当前初始强制延迟为 {self.initial_delay} 秒")
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
        
        # 如果消息已经被处理过，则跳过
        if getattr(event, "_has_been_handled_by_combiner", False):
            logger.debug(f"消息已被合并插件处理过，跳过: {event.message_str[:30]}...")
            return
            
        # 标记消息为已处理，防止重复处理
        setattr(event, "_has_been_handled_by_combiner", True)
            
        # 缓存消息
        try:
            await message_buffer.start_caching_messages(event)
        except AttributeError as e:
            logger.error(f"缓存消息时出错(属性不存在): {str(e)}")
            return
        except Exception as e:
            logger.error(f"缓存消息时出错: {str(e)}")
            return
        
        # 查询是否应该合并
        try:
            result, merged_text = await message_buffer.query_buffer_result(event)
        except Exception as e:
            logger.error(f"查询缓冲结果时出错: {str(e)}")
            return
        
        if result and merged_text and len(merged_text) > 0:
            # 这是一条被正确处理的T消息，如果需要合并，则修改内容
            if merged_text != event.message_str:
                # 修改原始消息内容，而不是直接回复
                logger.debug(f"修改消息文本: '{event.message_str[:30]}...' -> '{merged_text[:30]}...'")
                event.message_str = merged_text
                # 修改消息对象中的文本
                event.message_obj.message_str = merged_text
                # 更新消息组件
                event.message_obj.message = [Plain(merged_text)]
                logger.info(f"已合并消息: {merged_text[:50]}..." if len(merged_text) > 50 else f"已合并消息: {merged_text}")
        elif not result:
            # 如果不是T消息，那可能是F消息(被合并)或U消息(等待处理)
            # 为了安全起见，我们通过缓冲区检查一下此消息的状态
            platform = event.platform_meta.name
            user_id = event.get_sender_id()
            group_id = None
            if not event.is_private_chat():
                group_id = event.message_obj.group_id
                
            person_id_ = message_buffer.get_person_id_(platform, user_id, group_id)
            
            # 兼容性检查：如果message_obj没有message_id属性，则生成一个唯一id
            try:
                message_id = event.message_obj.message_id
            except AttributeError:
                # 使用时间戳和用户id生成唯一标识
                message_id = f"{platform}_{user_id}_{int(time.time() * 1000)}"
                logger.debug(f"未找到message_id，处理中使用临时id: {message_id}")
            
            async with message_buffer.lock:
                user_msgs = message_buffer.buffer_pool.get(person_id_, {})
                cache_msg = user_msgs.get(message_id)
                
                if cache_msg and cache_msg.result == "F":
                    # 这是一条被合并的消息，应该停止处理
                    try:
                        event.stop_event()
                        logger.debug(f"阻止了被合并的消息继续处理: {event.message_str[:30]}...")
                    except Exception as e:
                        logger.warning(f"无法停止消息事件: {str(e)}")
                elif cache_msg and cache_msg.result == "U":
                    # 这是一条尚未决定如何处理的消息，可能需要再等一下
                    logger.debug(f"消息仍在等待合并决策: {event.message_str[:30]}...")
                    
                    # 为了安全起见，我们标记这条消息为已处理
                    # 以防止重复处理
                    try:
                        if hasattr(event, "message"):
                            if not getattr(event.message, "_has_send_oper", False):
                                setattr(event.message, "_has_send_oper", True)
                        else:
                            if not getattr(event, "_has_send_oper", False):
                                setattr(event, "_has_send_oper", True)
                    except Exception as e:
                        logger.debug(f"设置消息处理标记失败: {str(e)}")
    
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
                
            # 如果消息已经被处理过，则跳过
            if getattr(event, "_early_intercepted", False):
                return True  # 继续处理
                
            # 标记消息为已经过早期拦截
            setattr(event, "_early_intercepted", True)
                
            # 检查是否是被合并的消息
            platform = event.platform_meta.name
            user_id = event.get_sender_id()
            group_id = None
            if not event.is_private_chat():
                group_id = event.message_obj.group_id
                
            person_id_ = message_buffer.get_person_id_(platform, user_id, group_id)
            
            # 兼容性检查：如果message_obj没有message_id属性，则生成一个唯一id
            try:
                message_id = event.message_obj.message_id
            except AttributeError:
                # 使用时间戳和用户id生成唯一标识
                message_id = f"{platform}_{user_id}_{int(time.time() * 1000)}"
                logger.debug(f"未找到message_id，早期拦截使用临时id: {message_id}")
            
            # 添加初始强制延迟，给后续可能的消息一个追上的机会
            # 这是最核心的修复，保证所有消息都有一个最小的合并窗口
            if not getattr(event, "_init_delayed", False):
                setattr(event, "_init_delayed", True)
                logger.debug(f"早期拦截添加初始延迟: {message_buffer.initial_delay}秒")
                await asyncio.sleep(message_buffer.initial_delay)

            # 缓存消息但不处理结果，让后续的handle_message处理
            # 这里只是提前拦截可能的F类消息
            try:
                await message_buffer.start_caching_messages(event)
            except AttributeError as e:
                logger.error(f"早期拦截缓存消息时出错(属性不存在): {str(e)}")
                return True  # 出错时继续处理，让后续流程决定
            except Exception as e:
                logger.error(f"早期拦截缓存消息时出错: {str(e)}")
                return True  # 出错时继续处理，让后续流程决定
            
            async with message_buffer.lock:
                user_msgs = message_buffer.buffer_pool.get(person_id_, {})
                cache_msg = user_msgs.get(message_id)
                
                if cache_msg and cache_msg.result == "F":
                    # 这是一条被合并的消息，立即拦截
                    logger.debug(f"早期拦截被合并的消息: {event.message_str[:30]}...")
                    
                    # 标记为已拦截，防止后续重复处理
                    try:
                        if hasattr(event, "message"):
                            if not getattr(event.message, "_has_send_oper", False):
                                setattr(event.message, "_has_send_oper", True)
                        else:
                            if not getattr(event, "_has_send_oper", False):
                                setattr(event, "_has_send_oper", True)
                    except Exception as e:
                        logger.debug(f"设置消息处理标记失败: {str(e)}")
                    return False  # 停止处理
            
            return True  # 继续处理
        
        # 注册早期拦截函数
        if hasattr(self.context, "register_early_intercept"):
            self.context.register_early_intercept(early_intercept)
            logger.info("成功注册早期消息拦截Hook")
        else:
            logger.warning("无法注册早期消息拦截Hook，合并功能可能不完全生效")