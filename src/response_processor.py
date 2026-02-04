"""
响应处理模块
处理流式和非流式响应的所有逻辑
"""
import json
import time
import asyncio
import logging
import uuid
import re
from datetime import datetime
from typing import Dict, AsyncGenerator, Tuple, Optional
import pytz
import httpx

from src.constants import (
    APIConstants, ResponseConstants, ContentConstants, 
    NumericConstants, TimeConstants, HeaderConstants
)
from src.exceptions import UpstreamError, TimeoutError as ProxyTimeoutError
from src.utils import safe_log_error, safe_log_info, safe_log_warning
from src.toolify_config import get_toolify
from src.toolify.detector import StreamingFunctionCallDetector

logger = logging.getLogger(__name__)

class ResponseProcessor:
    """响应处理器"""
    
    def __init__(self, config):
        self.config = config
    
    def extract_thinking_content(self, full_content: str) -> Tuple[Optional[str], str, Optional[int]]:
        """
        从完整内容中提取思考内容、回答内容和思考时长
        
        Args:
            full_content: 完整的响应内容
        
        Returns:
            (thinking_content, answer_content, duration)
            - thinking_content: 思考内容(如果存在)
            - answer_content: 回答内容
            - duration: 思考时长(秒,如果存在)
        """
        if not full_content:
            return None, "", None
        
        # 1. 尝试使用标准正则匹配 <think duration="...">...</think>
        match = re.search(ContentConstants.THINK_TAG_PATTERN, full_content, re.DOTALL)
        
        if match:
            duration_str = match.group(1)  # duration属性值(可能为None)
            thinking_content = match.group(2).strip()  # 思考内容
            duration = int(duration_str) if duration_str else None
            
            # 移除<think>标签后的剩余内容作为回答
            answer_content = full_content[:match.start()] + full_content[match.end():]
            answer_content = answer_content.strip()
            
            return thinking_content, answer_content, duration
            
        # 2. 容错处理: 只有 </think> 结束标签而没有开始标签的情况
        # 观察到上游有时会返回缺失 <think> 开始标签但包含 </think> 的内容
        elif ContentConstants.THINK_END_TAG in full_content:
            end_index = full_content.find(ContentConstants.THINK_END_TAG)
            
            # 确保这不是一个带有开始标签但正则没匹配上的情况
            start_tag_index = full_content.find("<think")
            
            # 如果确实没有开始标签，或者结束标签在开始标签之前(异常情况)
            if start_tag_index == -1 or end_index < start_tag_index:
                thinking_content = full_content[:end_index].strip()
                answer_content = full_content[end_index + len(ContentConstants.THINK_END_TAG):].strip()
                return thinking_content, answer_content, None
        
        # 3. 没有发现思考标签，整个内容即为回答
        return None, full_content, None
    
    def extract_answer_content(self, full_content: str, output_thinking: bool = True) -> str:
        """删除第一个<answer>标签和最后一个</answer>标签，保留内容"""
        if not full_content:
            return full_content
        
        # 完全通过模型名控制思考内容输出，默认显示思考内容
        should_output_thinking = output_thinking
        
        if should_output_thinking:
            # 删除第一个<answer>
            answer_start = full_content.find(ContentConstants.ANSWER_START_TAG)
            if answer_start != -1:
                full_content = full_content[:answer_start] + full_content[answer_start + len(ContentConstants.ANSWER_START_TAG):]

            # 删除最后一个</answer>
            answer_end = full_content.rfind(ContentConstants.ANSWER_END_TAG)
            if answer_end != -1:
                full_content = full_content[:answer_end] + full_content[answer_end + len(ContentConstants.ANSWER_END_TAG):]

            return full_content.strip()
        else:
            # 删除<think>部分（包括标签）
            think_start = full_content.find(ContentConstants.THINK_START_TAG)
            think_end = full_content.find(ContentConstants.THINK_END_TAG)
            if think_start != -1 and think_end != -1:
                full_content = full_content[:think_start] + full_content[think_end + len(ContentConstants.THINK_END_TAG):]
            
            # 删除<answer>标签及其内容之外的部分
            answer_start = full_content.find(ContentConstants.ANSWER_START_TAG)
            answer_end = full_content.rfind(ContentConstants.ANSWER_END_TAG)
            if answer_start != -1 and answer_end != -1:
                content = full_content[answer_start + len(ContentConstants.ANSWER_START_TAG):answer_end]
                return content.strip()

            return full_content.strip()
    
    def calculate_dynamic_chunk_size(self, content_length: int) -> int:
        """
        动态计算流式输出的chunk大小
        确保总输出时间不超过MAX_STREAM_TIME秒
        
        Args:
            content_length: 待输出内容的总长度
        
        Returns:
            int: 动态计算的chunk大小，最小为50
        """
        if content_length <= 0:
            return self.config.STREAM_CHUNK_SIZE
        
        # 计算需要的总chunk数量以满足时间限制
        # 总时间 = chunk数量 * STREAM_DELAY
        # chunk数量 = content_length / chunk_size
        # 所以：总时间 = (content_length / chunk_size) * STREAM_DELAY
        # 解出：chunk_size = (content_length * STREAM_DELAY) / MAX_STREAM_TIME
        
        calculated_chunk_size = int((content_length * self.config.STREAM_DELAY) / self.config.MAX_STREAM_TIME)
        
        # 确保chunk_size不小于最小值
        dynamic_chunk_size = max(calculated_chunk_size, NumericConstants.MIN_CHUNK_SIZE)
        
        # 如果计算出的chunk_size太大（比如内容很短），使用默认值
        if dynamic_chunk_size > content_length:
            dynamic_chunk_size = min(self.config.STREAM_CHUNK_SIZE, content_length)
        
        logger.debug(f"动态chunk_size计算: 内容长度={content_length}, 计算值={calculated_chunk_size}, 最终值={dynamic_chunk_size}")
        
        return dynamic_chunk_size
    
    def content_to_multimodal(self, content) -> str | list[dict]:
        """将内容转换为多模态格式用于K2Think API"""
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # 检查是否包含图像内容
            has_image = False
            result_parts = []
            
            for p in content:
                if hasattr(p, 'type'):  # ContentPart object
                    if getattr(p, 'type') == ContentConstants.TEXT_TYPE and getattr(p, 'text', None):
                        result_parts.append({
                            "type": ContentConstants.TEXT_TYPE,
                            "text": getattr(p, 'text')
                        })
                    elif getattr(p, 'type') == ContentConstants.IMAGE_URL_TYPE and getattr(p, 'image_url', None):
                        has_image = True
                        image_url_obj = getattr(p, 'image_url')
                        if hasattr(image_url_obj, 'url'):
                            url = getattr(image_url_obj, 'url')
                        else:
                            url = image_url_obj.get('url') if isinstance(image_url_obj, dict) else str(image_url_obj)
                        
                        result_parts.append({
                            "type": ContentConstants.IMAGE_URL_TYPE,
                            "image_url": {
                                "url": url
                            }
                        })
                elif isinstance(p, dict):
                    if p.get("type") == ContentConstants.TEXT_TYPE and p.get("text"):
                        result_parts.append({
                            "type": ContentConstants.TEXT_TYPE, 
                            "text": p.get("text")
                        })
                    elif p.get("type") == ContentConstants.IMAGE_URL_TYPE and p.get("image_url"):
                        has_image = True
                        result_parts.append({
                            "type": ContentConstants.IMAGE_URL_TYPE,
                            "image_url": p.get("image_url")
                        })
                elif isinstance(p, str):
                    result_parts.append({
                        "type": ContentConstants.TEXT_TYPE,
                        "text": p
                    })
            
            # 如果包含图像，返回多模态格式；否则返回纯文本
            if has_image and result_parts:
                return result_parts
            else:
                # 提取所有文本内容
                text_parts = []
                for part in result_parts:
                    if part.get("type") == ContentConstants.TEXT_TYPE:
                        text_parts.append(part.get("text", ""))
                return " ".join(text_parts)
        
        # 处理其他类型
        try:
            return str(content)
        except:
            return ""
    
    def get_current_datetime_info(self) -> Dict[str, str]:
        """获取当前时间信息"""
        # 设置时区为上海
        tz = pytz.timezone(ContentConstants.DEFAULT_TIMEZONE)
        now = datetime.now(tz)
        
        return {
            "{{USER_NAME}}": ContentConstants.DEFAULT_USER_NAME,
            "{{USER_LOCATION}}": ContentConstants.DEFAULT_USER_LOCATION,
            "{{CURRENT_DATETIME}}": now.strftime(TimeConstants.DATETIME_FORMAT),
            "{{CURRENT_DATE}}": now.strftime(TimeConstants.DATE_FORMAT),
            "{{CURRENT_TIME}}": now.strftime(TimeConstants.TIME_FORMAT),
            "{{CURRENT_WEEKDAY}}": now.strftime(TimeConstants.WEEKDAY_FORMAT),
            "{{CURRENT_TIMEZONE}}": ContentConstants.DEFAULT_TIMEZONE,
            "{{USER_LANGUAGE}}": ContentConstants.DEFAULT_USER_LANGUAGE
        }
    
    def generate_session_id(self) -> str:
        """生成会话ID"""
        return str(uuid.uuid4())
    
    def generate_chat_id(self) -> str:
        """生成聊天ID"""
        return str(uuid.uuid4())
    
    async def create_http_client(self) -> httpx.AsyncClient:
        """创建HTTP客户端"""
        base_kwargs = {
            "timeout": httpx.Timeout(timeout=None, connect=10.0),
            "limits": httpx.Limits(
                max_keepalive_connections=self.config.MAX_KEEPALIVE_CONNECTIONS, 
                max_connections=self.config.MAX_CONNECTIONS
            ),
            "follow_redirects": True
        }
        
        try:
            return httpx.AsyncClient(**base_kwargs)
        except Exception as e:
            safe_log_error(logger, "创建客户端失败", e)
            raise e
    
    async def make_request(
        self, 
        method: str, 
        url: str, 
        headers: dict, 
        json_data: dict = None, 
        stream: bool = False
    ) -> httpx.Response:
        """发送HTTP请求"""
        client = None
        
        try:
            client = await self.create_http_client()
            
            if stream:
                # 流式请求返回context manager
                return client.stream(method, url, headers=headers, json=json_data, timeout=None)
            else:
                response = await client.request(
                    method, url, headers=headers, json=json_data, 
                    timeout=self.config.REQUEST_TIMEOUT
                )
                
                # 详细记录非200响应
                if response.status_code != APIConstants.HTTP_OK:
                    safe_log_error(logger, f"上游API返回错误状态码: {response.status_code}")
                    safe_log_error(logger, f"响应头: {dict(response.headers)}")
                    try:
                        error_body = response.text
                        safe_log_error(logger, f"错误响应体: {error_body}")
                    except:
                        safe_log_error(logger, "无法读取错误响应体")
                
                response.raise_for_status()
                return response
                
        except httpx.HTTPStatusError as e:
            safe_log_error(logger, f"HTTP状态错误: {e.response.status_code} - {e.response.text}")
            if client and not stream:
                await client.aclose()
            raise UpstreamError(f"上游服务错误: {e.response.status_code}", e.response.status_code)
        except httpx.TimeoutException as e:
            safe_log_error(logger, "请求超时", e)
            if client and not stream:
                await client.aclose()
            raise ProxyTimeoutError("请求超时")
        except Exception as e:
            safe_log_error(logger, "请求异常", e)
            if client and not stream:
                await client.aclose()
            raise e
    
    async def process_non_stream_response(self, k2think_payload: dict, headers: dict, output_thinking: bool = None) -> Tuple[str, dict, Optional[str], Optional[int]]:
        """处理非流式响应
        
        Returns:
            (answer_content, token_info, thinking_content, duration)
        """
        try:
            response = await self.make_request(
                "POST", 
                self.config.K2THINK_API_URL, 
                headers, 
                k2think_payload, 
                stream=False
            )
            
            # K2Think 非流式请求返回标准JSON格式
            result = response.json()
            
            # 提取原始内容
            raw_content = ""
            if result.get('choices') and len(result['choices']) > 0:
                choice = result['choices'][0]
                if choice.get('message') and choice['message'].get('content'):
                    raw_content = choice['message']['content']
            
            # 提取思考内容和回答内容
            thinking_content, answer_content, duration = self.extract_thinking_content(raw_content)
            
            # answer_content已经移除了<think>标签,现在只需要处理<answer>标签
            # 直接移除<answer>和</answer>标签,保留内容
            final_content = answer_content
            
            # 处理<answer>标签(如果存在)
            answer_start = final_content.find(ContentConstants.ANSWER_START_TAG)
            if answer_start != -1:
                final_content = final_content[:answer_start] + final_content[answer_start + len(ContentConstants.ANSWER_START_TAG):]
            
            answer_end = final_content.rfind(ContentConstants.ANSWER_END_TAG)
            if answer_end != -1:
                final_content = final_content[:answer_end] + final_content[answer_end + len(ContentConstants.ANSWER_END_TAG):]
            
            final_content = final_content.strip()
            
            # 提取token信息
            token_info = result.get('usage', {
                "prompt_tokens": NumericConstants.DEFAULT_PROMPT_TOKENS, 
                "completion_tokens": NumericConstants.DEFAULT_COMPLETION_TOKENS, 
                "total_tokens": NumericConstants.DEFAULT_TOTAL_TOKENS
            })
            
            await response.aclose()
            return final_content, token_info, thinking_content, duration
                        
        except Exception as e:
            safe_log_error(logger, "处理非流式响应错误", e)
            raise
    
    async def process_stream_response(
        self, 
        k2think_payload: dict, 
        headers: dict,
        output_thinking: bool = None,
        original_model: str = None,
        enable_toolify: bool = False
    ) -> AsyncGenerator[str, None]:
        """处理流式响应"""
        try:
            # 发送开始chunk
            start_chunk = self._create_chunk_data(
                delta={"role": "assistant", "content": ""},
                finish_reason=None,
                model=original_model
            )
            yield f"{ResponseConstants.STREAM_DATA_PREFIX}{json.dumps(start_chunk)}\n\n"
            
            # 优化的模拟流式输出 - 立即开始获取响应并流式发送
            k2think_payload_copy = k2think_payload.copy()
            k2think_payload_copy["stream"] = False
            
            headers_copy = headers.copy()
            headers_copy[HeaderConstants.ACCEPT] = HeaderConstants.APPLICATION_JSON
            
            # 获取完整响应(包括思考内容)
            answer_content, token_info, thinking_content, duration = await self.process_non_stream_response(
                k2think_payload_copy, headers_copy, output_thinking
            )
            
            # 如果没有内容,直接结束
            if not answer_content and not thinking_content:
                yield ResponseConstants.STREAM_DONE_MARKER
                return
            
            # 检测工具调用（如果启用）
            toolify_detector = None
            if enable_toolify:
                toolify = get_toolify()
                if toolify:
                    toolify_detector = StreamingFunctionCallDetector(toolify.trigger_signal)
                    safe_log_info(logger, "[TOOLIFY] 流式工具调用检测器已初始化")
            
            # 先发送思考内容(reasoning_content),再发送回答内容
            if thinking_content and output_thinking:
                # 流式输出思考内容到reasoning_content字段
                async for chunk in self._stream_reasoning_content(thinking_content, original_model):
                    yield chunk
            
            # 发送回答内容（支持工具调用检测）
            if toolify_detector:
                # 使用工具调用检测器处理内容
                async for chunk in self._stream_content_with_tool_detection(
                    answer_content, original_model, toolify_detector, k2think_payload.get("chat_id", "")
                ):
                    yield chunk
            else:
                # 正常流式发送回答内容
                async for chunk in self._stream_content(answer_content, original_model):
                    yield chunk
                
                # 发送结束chunk
                end_chunk = self._create_chunk_data(
                    delta={},
                    finish_reason=ResponseConstants.FINISH_REASON_STOP,
                    model=original_model
                )
                yield f"{ResponseConstants.STREAM_DATA_PREFIX}{json.dumps(end_chunk)}\n\n"
                yield ResponseConstants.STREAM_DONE_MARKER
            
        except Exception as e:
            safe_log_error(logger, "流式响应处理错误", e)
            
            # 发送错误信息作为流式响应的一部分，而不是抛出异常
            if "401" in str(e) or "unauthorized" in str(e).lower():
                # 401错误：显示tokens强制刷新消息
                error_message = "🔄 tokens强制刷新已启动，请稍后再试"
                safe_log_info(logger, "检测到401错误，向客户端发送强制刷新提示")
            else:
                # 其他错误：显示一般错误信息
                error_message = f"请求处理失败: {str(e)}"
            
            # 发送错误内容作为正常的流式响应
            error_chunk = self._create_chunk_data(
                delta={"content": f"\n\n{error_message}"},
                finish_reason=None,
                model=original_model
            )
            yield f"{ResponseConstants.STREAM_DATA_PREFIX}{json.dumps(error_chunk)}\n\n"
            
            # 发送结束chunk
            end_chunk = self._create_chunk_data(
                delta={},
                finish_reason=ResponseConstants.FINISH_REASON_ERROR,
                model=original_model
            )
            yield f"{ResponseConstants.STREAM_DATA_PREFIX}{json.dumps(end_chunk)}\n\n"
            yield ResponseConstants.STREAM_DONE_MARKER
            
            # 重新抛出异常以便上层处理token失败（在发送友好消息之后）
            # 上层会捕获这个异常并调用token_manager.mark_token_failure
            raise e
    
    async def _stream_reasoning_content(self, reasoning_content: str, model: str = None) -> AsyncGenerator[str, None]:
        """流式发送推理内容到reasoning_content字段"""
        chunk_size = self.calculate_dynamic_chunk_size(len(reasoning_content))
        
        for i in range(0, len(reasoning_content), chunk_size):
            chunk_content = reasoning_content[i:i + chunk_size]
            
            chunk = self._create_chunk_data(
                delta={"reasoning_content": chunk_content},
                finish_reason=None,
                model=model
            )
            
            yield f"{ResponseConstants.STREAM_DATA_PREFIX}{json.dumps(chunk)}\n\n"
            # 添加延迟模拟真实流式效果
            await asyncio.sleep(self.config.STREAM_DELAY)
    
    async def _stream_content(self, content: str, model: str = None) -> AsyncGenerator[str, None]:
        """流式发送内容"""
        chunk_size = self.calculate_dynamic_chunk_size(len(content))
        
        for i in range(0, len(content), chunk_size):
            chunk_content = content[i:i + chunk_size]
            
            chunk = self._create_chunk_data(
                delta={"content": chunk_content},
                finish_reason=None,
                model=model
            )
            
            yield f"{ResponseConstants.STREAM_DATA_PREFIX}{json.dumps(chunk)}\n\n"
            # 添加延迟模拟真实流式效果
            await asyncio.sleep(self.config.STREAM_DELAY)
    
    async def _stream_content_with_tool_detection(
        self, 
        content: str, 
        model: str, 
        detector: StreamingFunctionCallDetector,
        chat_id: str
    ) -> AsyncGenerator[str, None]:
        """流式发送内容并检测工具调用"""
        chunk_size = self.calculate_dynamic_chunk_size(len(content))
        
        for i in range(0, len(content), chunk_size):
            chunk_content = content[i:i + chunk_size]
            
            # 使用检测器处理chunk
            is_tool_detected, content_to_yield = detector.process_chunk(chunk_content)
            
            if is_tool_detected:
                safe_log_info(logger, "[TOOLIFY] 检测到工具调用触发信号")
            
            # 输出处理后的内容
            if content_to_yield:
                chunk = self._create_chunk_data(
                    delta={"content": content_to_yield},
                    finish_reason=None,
                    model=model
                )
                yield f"{ResponseConstants.STREAM_DATA_PREFIX}{json.dumps(chunk)}\n\n"
            
            await asyncio.sleep(self.config.STREAM_DELAY)
        
        # 流结束时的最终处理
        parsed_tools, remaining_content = detector.finalize()
        
        # 输出剩余内容
        if remaining_content:
            safe_log_info(logger, f"[TOOLIFY] 输出缓冲区剩余内容: {len(remaining_content)}字符")
            chunk = self._create_chunk_data(
                delta={"content": remaining_content},
                finish_reason=None,
                model=model
            )
            yield f"{ResponseConstants.STREAM_DATA_PREFIX}{json.dumps(chunk)}\n\n"
        
        # 如果检测到工具调用，输出工具调用结果
        if parsed_tools:
            safe_log_info(logger, f"[TOOLIFY] 检测到 {len(parsed_tools)} 个工具调用")
            from src.toolify_handler import format_toolify_response_for_stream
            tool_chunks = format_toolify_response_for_stream(parsed_tools, model, chat_id)
            for chunk in tool_chunks:
                yield chunk
        else:
            # 没有工具调用，正常结束
            end_chunk = self._create_chunk_data(
                delta={},
                finish_reason=ResponseConstants.FINISH_REASON_STOP,
                model=model
            )
            yield f"{ResponseConstants.STREAM_DATA_PREFIX}{json.dumps(end_chunk)}\n\n"
            yield ResponseConstants.STREAM_DONE_MARKER
    
    def _create_chunk_data(self, delta: dict, finish_reason: Optional[str], model: str = None) -> dict:
        """创建流式响应chunk数据"""
        return {
            "id": f"chatcmpl-{int(time.time() * 1000)}",
            "object": ResponseConstants.CHAT_COMPLETION_CHUNK_OBJECT,
            "created": int(time.time()),
            "model": model or APIConstants.MODEL_ID,
            "choices": [{
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason
            }]
        }
    
    def create_completion_response(
        self, 
        content: Optional[str],
        token_info: Optional[dict] = None,
        model: str = None,
        reasoning_content: Optional[str] = None,
        reasoning_duration: Optional[int] = None
    ) -> dict:
        """创建完整的聊天补全响应
        
        Args:
            content: 回答内容
            token_info: token使用信息
            model: 模型名称
            reasoning_content: 思考内容(可选)
            reasoning_duration: 思考时长秒数(可选)
        """
        message = {
            "role": "assistant",
            "content": content,
        }
        
        # 如果有思考内容,添加到message中
        if reasoning_content:
            message["reasoning_content"] = reasoning_content
        
        # 处理token统计
        usage = token_info or {
            "prompt_tokens": NumericConstants.DEFAULT_PROMPT_TOKENS,
            "completion_tokens": NumericConstants.DEFAULT_COMPLETION_TOKENS,
            "total_tokens": NumericConstants.DEFAULT_TOTAL_TOKENS
        }
        
        # 如果有思考时长,添加reasoning_tokens估算
        # 按照约1字/token的比例估算(中文约3字符/token,英文约4字符/token)
        if reasoning_content and "completion_tokens_details" not in usage:
            reasoning_tokens = len(reasoning_content) // 2  # 粗略估算
            usage["completion_tokens_details"] = {
                "reasoning_tokens": reasoning_tokens
            }
        
        return {
            "id": f"chatcmpl-{int(time.time())}",
            "object": ResponseConstants.CHAT_COMPLETION_OBJECT,
            "created": int(time.time()),
            "model": model or APIConstants.MODEL_ID,
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": ResponseConstants.FINISH_REASON_STOP
            }],
            "usage": usage
        }