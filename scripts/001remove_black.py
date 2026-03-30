#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频黑屏帧检测与删除工具
自动检测并删除视频开头和结尾的黑屏帧（包括淡入淡出）
支持使用 Kimi AI 进行智能黑幕和渐入效果检测
"""

import cv2
import numpy as np
import subprocess
import os
import sys
import base64
import json
from pathlib import Path
from typing import Tuple, Optional, List, Dict
import requests

# 设置 Windows 控制台编码为 UTF-8
if sys.platform == 'win32':
    try:
        import codecs
        sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
        sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')
    except:
        pass


class KimiAIDetector:
    """使用 Kimi AI 进行智能黑幕和渐入效果检测"""
    
    def __init__(self, api_key: Optional[str] = None):
        """
        初始化 Kimi AI 检测器
        
        Args:
            api_key: Kimi API 密钥；未传入时从环境变量 KIMI_API_KEY 读取
        """
        self.api_key = api_key or os.getenv("KIMI_API_KEY")
        self.api_url = "https://api.moonshot.cn/v1/chat/completions"
        self.model = "kimi-k2.5"
    
    def encode_frame_to_base64(self, frame, quality: int = 60) -> str:
        """
        将视频帧编码为 base64 字符串
        
        Args:
            frame: 视频帧
            quality: JPEG 质量（1-100），默认 60 以减小文件大小
        """
        # 缩小图片尺寸以减少传输时间
        height, width = frame.shape[:2]
        if width > 640:
            scale = 640 / width
            new_width = 640
            new_height = int(height * scale)
            frame = cv2.resize(frame, (new_width, new_height))
        
        # 使用较低的 JPEG 质量以减小文件大小
        encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), quality]
        _, buffer = cv2.imencode('.jpg', frame, encode_param)
        return base64.b64encode(buffer).decode('utf-8')
    
    def analyze_frames(self, frames: List[np.ndarray], frame_indices: List[int]) -> Dict:
        """
        使用 Kimi AI 分析多个视频帧，判断是否为黑幕或渐入效果
        
        Args:
            frames: 视频帧列表
            frame_indices: 帧索引列表
            
        Returns:
            分析结果字典，包含每帧的判断结果
        """
        if not frames:
            return {}
        
        # 编码所有帧为 base64
        encoded_frames = []
        for i, frame in enumerate(frames):
            encoded = self.encode_frame_to_base64(frame)
            encoded_frames.append({
                "index": frame_indices[i],
                "image": encoded
            })
        
        # 构建提示词 - 直接要求 JSON，不要思考过程
        prompt = f"""直接返回JSON，不要解释。分析 {len(frames)} 个视频帧：

类型：pure_black（纯黑）、fade_in（渐入）、normal（正常）

JSON格式（必须包含全部 {len(frames)} 帧）：
{{
  "frames": [
    {{"index": 0, "type": "pure_black", "confidence": 0.95, "reason": "黑"}},
    {{"index": 1, "type": "fade_in", "confidence": 0.85, "reason": "渐入"}},
    {{"index": 2, "type": "normal", "confidence": 0.90, "reason": "正常"}}
  ]
}}

只返回JSON，不要其他内容。"""
        
        # 构建消息内容（包含图片）
        content = [{"type": "text", "text": prompt}]
        
        # 添加图片（Kimi 支持多图分析）
        # 限制最多12帧，确保能覆盖黑幕、渐入和正常内容
        for item in encoded_frames[:12]:
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{item['image']}"
                }
            })
        
        # 调用 Kimi API（带重试机制）
        max_retries = 3
        retry_delay = 2  # 秒
        
        for attempt in range(max_retries):
            try:
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                }
                
                payload = {
                    "model": self.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "你是一个视频帧分析助手。直接返回JSON格式的分析结果，不要包含任何解释、思考过程或其他文字。"
                        },
                        {
                            "role": "user",
                            "content": content
                        }
                    ],
                    "temperature": 1,  # kimi-k2.5 只支持 temperature=1
                    "max_tokens": 4000  # 增加到4000，确保能返回完整结果
                }
                
                # 增加超时时间到 60 秒
                response = requests.post(
                    self.api_url,
                    headers=headers,
                    json=payload,
                    timeout=60
                )
                
                print(f"   📊 API 响应状态码: {response.status_code}")
                print(f"   📊 响应头: {dict(response.headers)}")
                
                if response.status_code == 200:
                    # 打印原始响应文本
                    raw_text = response.text
                    print(f"   📝 原始响应文本长度: {len(raw_text)} 字符")
                    if len(raw_text) < 500:
                        print(f"   📝 原始响应文本: {raw_text}")
                    
                    result = response.json()
                    print(f"   📝 JSON 解析后的键: {list(result.keys())}")
                    
                    # 检查响应结构
                    if 'choices' not in result or len(result['choices']) == 0:
                        print(f"   ⚠️  API 响应格式异常: {result}")
                        return {}
                    
                    print(f"   📝 choices 数量: {len(result['choices'])}")
                    print(f"   📝 第一个 choice 的键: {list(result['choices'][0].keys())}")
                    print(f"   📝 message 的键: {list(result['choices'][0]['message'].keys())}")
                    
                    # Kimi API 把内容放在 content 或 reasoning_content 中
                    message = result['choices'][0]['message']
                    
                    # 优先使用 content，如果为空则使用 reasoning_content
                    content_text = message.get('content', '')
                    reasoning_text = message.get('reasoning_content', '')
                    
                    # 打印调试信息
                    print(f"   📝 content 长度: {len(content_text)} 字符")
                    print(f"   📝 reasoning_content 长度: {len(reasoning_text)} 字符")
                    
                    # 如果 content 为空，使用 reasoning_content
                    if not content_text and reasoning_text:
                        content_text = reasoning_text
                        print(f"   ℹ️  使用 reasoning_content（{len(content_text)} 字符）")
                    
                    # 检查是否被截断
                    if 'finish_reason' in result['choices'][0]:
                        finish_reason = result['choices'][0]['finish_reason']
                        print(f"   📝 finish_reason: {finish_reason}")
                        if finish_reason == 'length':
                            print(f"   ⚠️  响应因长度限制被截断，需要增加 max_tokens")
                    
                    if len(content_text) > 1000:
                        print(f"   响应开头: {content_text[:300]}")
                        print(f"   响应结尾: {content_text[-300:]}")
                    else:
                        print(f"   完整响应: {content_text}")
                    
                    # 尝试解析 JSON 响应
                    try:
                        # 提取 JSON 部分（可能包含在 markdown 代码块中）
                        if "```json" in content_text:
                            json_start = content_text.find("```json") + 7
                            json_end = content_text.find("```", json_start)
                            if json_end > json_start:
                                content_text = content_text[json_start:json_end].strip()
                        elif "```" in content_text:
                            json_start = content_text.find("```") + 3
                            json_end = content_text.find("```", json_start)
                            if json_end > json_start:
                                content_text = content_text[json_start:json_end].strip()
                        
                        # 尝试直接解析（可能没有代码块包裹）
                        if content_text.strip().startswith('{'):
                            analysis = json.loads(content_text)
                            print(f"   ✅ AI 解析成功，识别了 {len(analysis.get('frames', []))} 个帧")
                            return analysis
                        
                        # 直接使用正则表达式提取帧信息，不依赖完整 JSON
                        import re
                        print(f"   🔍 使用正则表达式提取帧信息...")
                        
                        # 提取所有的 index、type、confidence、reason
                        frames_list = []
                        # 匹配格式: {"index": 0, "type": "pure_black", "confidence": 0.95, "reason": "..."}
                        frame_pattern = r'\{"index":\s*(\d+),\s*"type":\s*"([^"]+)",\s*"confidence":\s*([\d.]+),\s*"reason":\s*"([^"]*)"\}'
                        matches = re.finditer(frame_pattern, content_text)
                        
                        for match in matches:
                            frames_list.append({
                                "index": int(match.group(1)),
                                "type": match.group(2),
                                "confidence": float(match.group(3)),
                                "reason": match.group(4)
                            })
                        
                        if frames_list:
                            print(f"   ✅ 正则提取成功，识别了 {len(frames_list)} 个帧")
                            return {"frames": frames_list, "recommendation": ""}
                        
                        # 如果正则提取失败，尝试标准 JSON 解析
                        json_start = content_text.find('{')
                        json_end = content_text.rfind('}')
                        if json_start >= 0 and json_end > json_start:
                            json_str = content_text[json_start:json_end+1]
                            
                            # 清理省略符号
                            json_str = re.sub(r',\s*\.\.\.\s*[^,\]]*', '', json_str)  # 移除 ... 及其后续文字
                            json_str = re.sub(r'\.\.\.\s*一直到\d+', '', json_str)  # 移除中文省略
                            
                            print(f"   🔍 尝试标准 JSON 解析...")
                            
                            try:
                                analysis = json.loads(json_str)
                                print(f"   ✅ JSON 解析成功，识别了 {len(analysis.get('frames', []))} 个帧")
                                return analysis
                            except json.JSONDecodeError as e:
                                print(f"   ⚠️  JSON 解析失败: {str(e)[:100]}")
                                return
                        
                        print(f"   ⚠️  AI 响应中未找到有效的 JSON 格式")
                        return {}
                    except json.JSONDecodeError as e:
                        print(f"   ⚠️  AI 响应 JSON 解析失败: {str(e)[:100]}")
                        return {}
                    except Exception as e:
                        print(f"   ⚠️  AI 响应处理异常: {str(e)[:100]}")
                        return {}
                else:
                    error_msg = response.text if response.text else "未知错误"
                    print(f"   ⚠️  Kimi API 调用失败: {response.status_code}")
                    print(f"   错误详情: {error_msg[:200]}")
                    
                    # 如果是 4xx 错误，不重试
                    if 400 <= response.status_code < 500:
                        return {}
                    
                    # 5xx 错误，重试
                    if attempt < max_retries - 1:
                        print(f"   🔄 {retry_delay}秒后重试 ({attempt + 1}/{max_retries})...")
                        import time
                        time.sleep(retry_delay)
                        continue
                    return {}
                    
            except requests.exceptions.Timeout:
                print(f"   ⚠️  API 请求超时 (尝试 {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    print(f"   🔄 {retry_delay}秒后重试...")
                    import time
                    time.sleep(retry_delay)
                    continue
                return {}
            except Exception as e:
                print(f"   ⚠️  AI 分析异常: {e}")
                if attempt < max_retries - 1:
                    print(f"   🔄 {retry_delay}秒后重试...")
                    import time
                    time.sleep(retry_delay)
                    continue
                return {}
        
        return {}


class BlackFrameRemover:
    def __init__(self, brightness_threshold=50, fade_threshold=110, min_black_duration=0.1, check_text=True, use_ai=False, min_voice_db=-15.0):
        """
        初始化黑屏检测器

        Args:
            brightness_threshold: 黑屏亮度阈值(0-255)，低于此值认为是黑屏，默认50（包括淡入淡出）
            fade_threshold: 淡入淡出阈值(0-255)，低于此值认为是淡入淡出，默认110
            min_black_duration: 最小黑屏持续时间(秒)，短于此时间的黑屏不处理，默认0.1
            check_text: 是否检测文字，如果检测到文字则保留黑屏，默认True
            use_ai: 是否使用 Kimi AI 进行智能检测，默认False
            min_voice_db: 判定为人声的最低音量阈值(dB)，高于此值保留黑屏，默认-15（BGM通常<-20）
        """
        self.brightness_threshold = brightness_threshold
        self.fade_threshold = fade_threshold
        self.min_black_duration = min_black_duration
        self.check_text = check_text
        self.use_ai = use_ai
        self.min_voice_db = min_voice_db
        self.ai_detector = KimiAIDetector() if use_ai else None
    
    def has_audio_in_range(self, video_path: str, start_sec: float, end_sec: float,
                           min_volume_db: float = -40.0) -> bool:
        """
        检测视频指定时间段内是否有音频活动（人声对白等）。

        原理：用 ffmpeg volumedetect 滤镜测量该段最大音量，
        超过阈值（默认 -40 dB）认为有人声，不应删除。

        Args:
            video_path: 视频文件路径
            start_sec:  检测起始秒
            end_sec:    检测结束秒
            min_volume_db: 音量阈值（dB），高于此值认为有音频活动
        """
        if end_sec <= start_sec:
            return False

        duration = end_sec - start_sec
        cmd = [
            'ffmpeg', '-y',
            '-ss', str(start_sec),
            '-t', str(duration),
            '-i', video_path,
            '-vn',               # 只处理音频
            '-af', 'volumedetect',
            '-f', 'null', '-'    # null muxer，跨平台
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, timeout=30)
            # volumedetect 结果输出在 stderr（bytes，避免 Windows 编码问题）
            import re
            stderr_text = (result.stderr or b'').decode('utf-8', errors='replace')
            m = re.search(r'max_volume:\s*([-\d.]+)\s*dB', stderr_text)
            if m:
                max_vol = float(m.group(1))
                has_audio = max_vol > min_volume_db
                print(f"   🔊 音频检测 [{start_sec:.2f}s-{end_sec:.2f}s]: max_volume={max_vol:.1f}dB "
                      f"{'→ 有人声，保留' if has_audio else '→ 静音'}")
                return has_audio
        except Exception as e:
            print(f"   ⚠️  音频检测失败: {e}")

        return False

    def calculate_frame_brightness(self, frame) -> float:
        """计算帧的平均亮度"""
        # 转换为灰度图
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        # 计算平均亮度
        return np.mean(gray)
    
    def has_text_in_frame(self, frame) -> bool:
        """
        检测帧中是否有文字
        使用多种方法综合判断，要求更严格的阈值
        """
        try:
            # 转换为灰度图
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            
            # 计算平均亮度
            brightness = np.mean(gray)
            
            # 如果亮度太低（<60），即使有边缘也不算文字（可能是淡入中的文字）
            if brightness < 60:
                return False
            
            # 方法1: 检测高亮区域（文字通常比背景亮）+ 亮度检查
            _, bright_mask = cv2.threshold(gray, 100, 255, cv2.THRESH_BINARY)
            bright_pixels = np.sum(bright_mask > 0)
            total_pixels = gray.shape[0] * gray.shape[1]
            bright_ratio = bright_pixels / total_pixels
            
            # 亮像素比例要求更高（>5%）且整体亮度足够（>80）
            if bright_ratio > 0.05 and brightness > 80:
                return True
            
            # 方法2: 边缘检测 + 亮度检查
            edges = cv2.Canny(gray, 50, 150)
            edge_pixels = np.sum(edges > 0)
            edge_ratio = edge_pixels / total_pixels
            
            # 边缘比例要求更高（>1%）且亮度足够（>80）
            if edge_ratio > 0.01 and brightness > 80:
                return True
            
            return False
            
        except Exception as e:
            # 检测失败，保守起见认为没有文字（改为False，避免误判）
            return False
    
    def detect_black_segments(self, video_path: str) -> Tuple[Optional[float], Optional[float]]:
        """
        检测视频开头和结尾的黑屏段（包括淡入淡出）
        
        Returns:
            (start_trim, end_trim): 需要裁剪的开始和结束时间(秒)
            如果没有黑屏则返回 (None, None)
        """
        print(f"\n🔍 分析视频: {Path(video_path).name}")
        
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            print(f"❌ 无法打开视频: {video_path}")
            return None, None
        
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = total_frames / fps if fps > 0 else 0
        
        print(f"   视频信息: {total_frames}帧, {fps:.2f}fps, {duration:.2f}秒")
        
        # 检测开头黑屏和淡入
        start_trim = None
        frame_idx = 0
        min_black_frames = max(1, int(self.min_black_duration * fps))
        
        # 如果启用 AI 检测，使用 AI 分析前几秒的帧
        if self.use_ai and self.ai_detector:
            print(f"   🤖 使用 Kimi AI 智能检测黑幕和渐入效果...")
            
            # 采样前3秒的帧（每0.3秒采样一帧，约10帧）
            sample_frames = []
            sample_indices = []
            sample_interval = int(fps * 0.3)  # 每0.3秒采样一帧
            max_sample_frames = min(int(fps * 3), total_frames)  # 最多采样3秒
            
            for idx in range(0, max_sample_frames, sample_interval):
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ret, frame = cap.read()
                if ret:
                    sample_frames.append(frame)
                    sample_indices.append(idx)
            
            print(f"   📸 采样了 {len(sample_frames)} 个帧（每0.3s一帧，前3秒）")
            
            # 使用 AI 分析
            if sample_frames:
                ai_result = self.ai_detector.analyze_frames(sample_frames, sample_indices)
                
                if ai_result and 'frames' in ai_result:
                    print(f"   🤖 AI 分析结果:")
                    
                    # 找到第一个完全正常的帧
                    first_normal_frame = -1
                    last_bad_frame = -1  # 最后一个黑幕或渐入帧
                    
                    for frame_info in ai_result['frames']:
                        frame_type = frame_info.get('type', 'normal')
                        actual_frame_idx = sample_indices[frame_info.get('index', 0)]  # 转换为实际帧索引
                        confidence = frame_info.get('confidence', 0)
                        reason = frame_info.get('reason', '')
                        
                        print(f"      帧{actual_frame_idx}: {frame_type} (置信度: {confidence:.2f}) - {reason}")
                        
                        # 记录最后一个黑幕或渐入帧
                        if frame_type in ['pure_black', 'fade_in']:
                            last_bad_frame = actual_frame_idx
                        # 找到第一个正常帧
                        elif frame_type == 'normal' and confidence > 0.85 and first_normal_frame < 0:
                            first_normal_frame = actual_frame_idx
                    
                    # 设置裁剪起始点 - 优先使用第一个 normal 帧，减少安全边距避免删除过多内容
                    if first_normal_frame >= 0:
                        # 从第一个 normal 帧开始，不增加额外安全边距（AI已经判断为正常帧）
                        start_frame = first_normal_frame
                        start_trim = start_frame / fps
                        print(f"   ✅ AI 检测到第一个正常帧在第 {first_normal_frame} 帧，从第 {start_frame} 帧（{start_trim:.2f}s）开始")
                    elif last_bad_frame >= 0:
                        # 如果没有找到 normal 帧，从最后一个坏帧的下一帧开始，增加0.3秒安全边距
                        safety_margin_frames = int(fps * 0.3)
                        start_frame = last_bad_frame + 1 + safety_margin_frames
                        start_trim = start_frame / fps
                        print(f"   ⚠️  AI 未检测到 normal 帧，从最后坏帧 {last_bad_frame} + 0.3s 安全边距，从第 {start_frame} 帧（{start_trim:.2f}s）开始")
                    else:
                        print(f"   ✅ AI 未检测到需要删除的黑幕或渐入效果")

                    # 检测 AI 确定的黑屏段内是否有人声/对白
                    if start_trim is not None and start_trim > 0:
                        if self.has_audio_in_range(video_path, 0.0, start_trim, self.min_voice_db):
                            print(f"   🔊 黑屏段（0s-{start_trim:.2f}s）内有人声，取消裁剪")
                            start_trim = None
                    
                    # AI 检测成功，跳到结尾检测
                    cap.release()
                    cap = cv2.VideoCapture(video_path)
                    
                    # 检测结尾黑屏
                    end_trim = None
                    start_check_frame = max(0, total_frames - int(5 * fps))
                    cap.set(cv2.CAP_PROP_POS_FRAMES, start_check_frame)
                    
                    print(f"   检测结尾纯黑屏...")
                    
                    frames_data = []
                    frame_idx = start_check_frame
                    
                    while frame_idx < total_frames:
                        ret, frame = cap.read()
                        if not ret:
                            break
                        
                        brightness = self.calculate_frame_brightness(frame)
                        has_text = False
                        if self.check_text and brightness < self.brightness_threshold:
                            has_text = self.has_text_in_frame(frame)
                        
                        frames_data.append((frame_idx, brightness, has_text))
                        frame_idx += 1
                    
                    cap.release()
                    
                    # 从后往前找第一个非黑屏帧
                    consecutive_black = 0
                    for i in range(len(frames_data) - 1, -1, -1):
                        frame_idx, brightness, has_text = frames_data[i]
                        
                        # 如果有文字，不算黑屏
                        if brightness < self.brightness_threshold and not has_text:
                            consecutive_black += 1
                        else:
                            # 找到最后一个非黑屏帧或有文字的帧
                            if consecutive_black >= min_black_frames:
                                end_trim = (frame_idx + 1) / fps
                                black_start = (frame_idx + 1) / fps
                                print(f"   ✅ 检测到结尾纯黑屏: {black_start:.2f}s -> {duration:.2f}s ({consecutive_black}帧)")
                            break
                    
                    if end_trim is None:
                        print(f"   ℹ️  结尾无纯黑屏")
                    
                    return start_trim, end_trim
                else:
                    print(f"   ⚠️  AI 分析失败，回退到传统方法")
                    # 回退到传统方法
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        
        # 如果没有使用 AI 或 AI 失败，使用传统方法
        if not self.use_ai or start_trim is None:
            print(f"   检测开头纯黑屏（保留有文字的黑屏）...")
        
        # 增强的黑幕和渐入检测
        black_end_frame = 0
        strict_black_end_frame = 0  # 严格纯黑结束帧（亮度<20），用于音频检测范围
        fade_in_end_frame = 0
        has_text_in_black = False

        # 第一步：找到纯黑屏段
        while frame_idx < min(total_frames, int(fps * 5)):
            ret, frame = cap.read()
            if not ret:
                break

            brightness = self.calculate_frame_brightness(frame)

            if brightness < self.brightness_threshold:
                if self.check_text and self.has_text_in_frame(frame):
                    has_text_in_black = True
                    print(f"   🔍 在黑屏中检测到文字（帧{frame_idx}），保留此段")
                    break
                black_end_frame = frame_idx + 1
                if brightness < 20:  # 严格纯黑（避免把渐入过渡帧计入音频检测范围）
                    strict_black_end_frame = frame_idx + 1
            else:
                if black_end_frame >= min_black_frames:
                    print(f"   🔍 检测到纯黑屏段: 0-{black_end_frame}帧 (0s-{black_end_frame/fps:.2f}s)")
                break

            frame_idx += 1
        
        # 第二步：从黑屏结束位置开始，检测渐入效果
        # 渐入效果特征：亮度逐渐增加，但还未达到正常水平
        if black_end_frame > 0 and not has_text_in_black:
            fade_in_end_frame = black_end_frame
            cap.set(cv2.CAP_PROP_POS_FRAMES, black_end_frame)
            
            # 检测接下来的帧，找到亮度稳定的正常帧
            # 降低亮度阈值，避免误判正常内容为渐入效果
            brightness_threshold_high = 80  # 正常内容的亮度阈值（从150降低到80）
            stable_brightness_count = 0
            required_stable_frames = int(fps * 0.2)  # 需要连续0.2秒的稳定亮度（从0.3秒减少到0.2秒）
            
            # 最多检测1秒（从3秒减少到1秒），避免删除过多正常内容
            for idx in range(black_end_frame, min(total_frames, black_end_frame + int(fps * 1))):
                ret, frame = cap.read()
                if not ret:
                    break
                
                brightness = self.calculate_frame_brightness(frame)
                
                # 如果亮度达到正常水平
                if brightness >= brightness_threshold_high:
                    stable_brightness_count += 1
                    if stable_brightness_count >= required_stable_frames:
                        # 找到稳定的正常帧
                        fade_in_end_frame = idx - required_stable_frames + 1
                        print(f"   🔍 检测到渐入效果: {black_end_frame}-{fade_in_end_frame}帧 ({black_end_frame/fps:.2f}s-{fade_in_end_frame/fps:.2f}s)")
                        break
                else:
                    stable_brightness_count = 0
                    fade_in_end_frame = idx + 1
            
            # 如果没有检测到明显的渐入效果，只删除纯黑屏部分
            if fade_in_end_frame > black_end_frame + int(fps * 0.5):
                # 如果渐入效果超过0.5秒，可能是误判，只删除黑屏部分
                fade_in_end_frame = black_end_frame
                print(f"   ⚠️  未检测到明显渐入效果，只删除纯黑屏部分")
        
        # 检测黑屏段是否包含人声/对白
        # 使用严格纯黑范围（亮度<20）做音频检测，避免把渐入过渡帧中的人声误判为"黑幕内有人声"
        has_audio_in_black = False
        if black_end_frame >= min_black_frames and not has_text_in_black:
            audio_end_frame = strict_black_end_frame if strict_black_end_frame >= min_black_frames else black_end_frame
            check_end = audio_end_frame / fps
            has_audio_in_black = self.has_audio_in_range(video_path, 0.0, check_end, self.min_voice_db)

        # 设置裁剪起始点
        if has_text_in_black or has_audio_in_black:
            start_trim = None
            reason = '文字' if has_text_in_black else '人声'
            print(f"   ✅ 黑屏中有{reason}，从0s开始")
        elif fade_in_end_frame > black_end_frame:
            start_trim = fade_in_end_frame / fps
            print(f"   ✅ 跳过黑屏和渐入效果，从{start_trim:.2f}s开始")
        elif black_end_frame >= min_black_frames:
            start_trim = black_end_frame / fps
            print(f"   ✅ 跳过纯黑屏，从{start_trim:.2f}s开始")
        else:
            print(f"   ℹ️  开头无黑屏，从0s开始")
        
        # 检测结尾黑屏
        end_trim = None
        start_check_frame = max(0, total_frames - int(5 * fps))
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_check_frame)
        
        print(f"   检测结尾纯黑屏...")
        
        frames_data = []
        frame_idx = start_check_frame
        
        while frame_idx < total_frames:
            ret, frame = cap.read()
            if not ret:
                break
            
            brightness = self.calculate_frame_brightness(frame)
            has_text = False
            if self.check_text and brightness < self.brightness_threshold:
                has_text = self.has_text_in_frame(frame)
            
            frames_data.append((frame_idx, brightness, has_text))
            frame_idx += 1
        
        cap.release()
        
        # 从后往前找第一个非黑屏帧
        consecutive_black = 0
        for i in range(len(frames_data) - 1, -1, -1):
            frame_idx, brightness, has_text = frames_data[i]
            
            # 如果有文字，不算黑屏
            if brightness < self.brightness_threshold and not has_text:
                consecutive_black += 1
            else:
                # 找到最后一个非黑屏帧或有文字的帧
                if consecutive_black >= min_black_frames:
                    end_trim = (frame_idx + 1) / fps
                    black_start = (frame_idx + 1) / fps
                    print(f"   ✅ 检测到结尾纯黑屏: {black_start:.2f}s -> {duration:.2f}s ({consecutive_black}帧)")
                break
        
        if end_trim is None:
            print(f"   ℹ️  结尾无纯黑屏")
        
        return start_trim, end_trim
    
    def trim_video(self, input_path: str, output_path: str, 
                   start_time: Optional[float] = None, 
                   end_time: Optional[float] = None) -> bool:
        """
        使用FFmpeg裁剪视频
        
        Args:
            input_path: 输入视频路径
            output_path: 输出视频路径
            start_time: 开始时间(秒)，None表示从头开始
            end_time: 结束时间(秒)，None表示到结尾
        """
        if start_time is None and end_time is None:
            print(f"   ℹ️  无需裁剪，跳过")
            return False
        
        cmd = ['ffmpeg', '-y', '-loglevel', 'error']
        
        # 先指定输入文件
        cmd.extend(['-i', input_path])
        
        # 添加开始时间（精确裁剪）
        if start_time is not None:
            cmd.extend(['-ss', str(start_time)])
        
        # 添加结束时间
        if end_time is not None:
            if start_time is not None:
                duration = end_time - start_time
            else:
                duration = end_time
            cmd.extend(['-t', str(duration)])
        
        # 使用重新编码模式实现精确裁剪（避免关键帧问题）
        # 使用libx264编码器，CRF 18保证高质量
        cmd.extend([
            '-c:v', 'libx264',
            '-crf', '18',
            '-preset', 'fast',
            '-c:a', 'aac',
            '-b:a', '192k',
            '-avoid_negative_ts', 'make_zero',  # 避免负时间戳
            '-fflags', '+genpts',  # 重新生成时间戳
            output_path
        ])
        
        print(f"   🎬 裁剪视频...")
        if start_time is not None:
            print(f"      开始: {start_time:.2f}s")
        if end_time is not None:
            print(f"      结束: {end_time:.2f}s")
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            
            if result.returncode == 0:
                file_size = os.path.getsize(output_path) / 1024 / 1024
                print(f"   ✅ 裁剪成功: {file_size:.2f}MB")
                return True
            else:
                print(f"   ❌ 裁剪失败: {result.stderr}")
                return False
                
        except subprocess.TimeoutExpired:
            print(f"   ❌ 裁剪超时")
            return False
        except Exception as e:
            print(f"   ❌ 裁剪异常: {e}")
            return False
    
    def process_video(self, video_path: str, output_dir: str = None) -> bool:
        """
        处理单个视频：检测并删除黑屏
        
        Args:
            video_path: 输入视频路径
            output_dir: 输出目录，None则使用默认目录
        """
        video_path = Path(video_path)
        
        if not video_path.exists():
            print(f"❌ 视频不存在: {video_path}")
            return False
        
        # 创建输出目录
        if output_dir is None:
            output_dir = Path('trimmed_videos')
        else:
            output_dir = Path(output_dir)
        
        output_dir.mkdir(exist_ok=True)
        
        # 输出文件路径
        output_path = output_dir / f"{video_path.stem}_trimmed{video_path.suffix}"
        
        # 检测黑屏
        start_trim, end_trim = self.detect_black_segments(str(video_path))
        
        # 裁剪视频
        if start_trim is not None or end_trim is not None:
            success = self.trim_video(str(video_path), str(output_path), start_trim, end_trim)
            if success:
                print(f"   📁 输出: {output_path}")
                return True
        else:
            print(f"   ℹ️  视频无需处理，复制原文件")
            import shutil
            shutil.copy2(video_path, output_path)
            return True
        
        return False
    
    def process_folder(self, folder_path: str, output_dir: str = None, 
                      extensions: tuple = ('.mp4', '.avi', '.mov', '.mkv', '.flv')):
        """
        批量处理文件夹中的所有视频
        
        Args:
            folder_path: 输入文件夹路径
            output_dir: 输出目录
            extensions: 要处理的视频文件扩展名
        """
        folder_path = Path(folder_path)
        
        if not folder_path.exists():
            print(f"❌ 文件夹不存在: {folder_path}")
            return
        
        # 查找所有视频文件
        video_files = []
        for ext in extensions:
            video_files.extend(folder_path.glob(f"*{ext}"))
        
        if not video_files:
            print(f"❌ 在 {folder_path} 中未找到视频文件")
            return
        
        print(f"\n{'='*60}")
        print(f"📂 批量处理: {len(video_files)} 个视频文件")
        print(f"{'='*60}")
        
        success_count = 0
        for i, video_file in enumerate(video_files, 1):
            print(f"\n[{i}/{len(video_files)}] 处理: {video_file.name}")
            if self.process_video(str(video_file), output_dir):
                success_count += 1
        
        print(f"\n{'='*60}")
        print(f"✨ 批量处理完成: {success_count}/{len(video_files)} 成功")
        print(f"{'='*60}\n")


def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description='检测并删除视频前后端的黑屏帧',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 处理单个视频
  python 001remove_black_frames.py video.mp4
  
  # 处理多个视频
  python 001remove_black_frames.py video1.mp4 video2.mp4 video3.mp4
  
  # 批量处理文件夹
  python 001remove_black_frames.py assets/ -o output/
  
  # 自定义阈值
  python 001remove_black_frames.py video.mp4 --threshold 15 --min-duration 1.0
        """
    )
    
    parser.add_argument('input', nargs='+', help='输入视频文件或文件夹路径（支持多个）')
    parser.add_argument('-o', '--output', help='输出目录 (默认: trimmed_videos/)')
    parser.add_argument('-t', '--threshold', type=int, default=50,
                       help='黑屏亮度阈值 0-255 (默认: 50，包括淡入淡出)')
    parser.add_argument('-f', '--fade-threshold', type=int, default=110,
                       help='淡入淡出亮度阈值 0-255 (默认: 110)')
    parser.add_argument('--no-text-check', action='store_true',
                       help='禁用文字检测（将剪掉所有黑屏，包括带文字的）')
    parser.add_argument('-d', '--min-duration', type=float, default=0.1,
                       help='最小黑屏持续时间(秒) (默认: 0.1)')
    parser.add_argument('--use-ai', action='store_true',
                       help='使用 Kimi AI 进行智能黑幕和渐入效果检测（更准确但需要网络）')
    parser.add_argument('--min-voice-db', type=float, default=-15.0,
                       help='判定为人声的最低音量阈值(dB)，高于此值保留黑屏 (默认: -15，BGM通常<-20dB)')
    
    args = parser.parse_args()
    
    # 创建处理器
    remover = BlackFrameRemover(
        brightness_threshold=args.threshold,
        fade_threshold=args.fade_threshold,
        min_black_duration=args.min_duration,
        check_text=not args.no_text_check,
        use_ai=args.use_ai,
        min_voice_db=args.min_voice_db
    )
    
    # 处理输入路径列表
    input_paths = args.input
    
    # 如果只有一个路径且是文件夹，批量处理文件夹
    if len(input_paths) == 1:
        input_path = Path(input_paths[0])
        if input_path.is_dir():
            remover.process_folder(str(input_path), args.output)
            return
    
    # 否则处理所有指定的文件
    print(f"\n{'='*60}")
    print(f"📂 批量处理: {len(input_paths)} 个视频文件")
    print(f"{'='*60}")
    
    success_count = 0
    for i, video_path in enumerate(input_paths, 1):
        video_path = Path(video_path)
        
        if not video_path.exists():
            print(f"\n[{i}/{len(input_paths)}] ❌ 文件不存在: {video_path}")
            continue
        
        if not video_path.is_file():
            print(f"\n[{i}/{len(input_paths)}] ❌ 不是文件: {video_path}")
            continue
        
        print(f"\n[{i}/{len(input_paths)}] 处理: {video_path.name}")
        if remover.process_video(str(video_path), args.output):
            success_count += 1
    
    print(f"\n{'='*60}")
    print(f"✨ 批量处理完成: {success_count}/{len(input_paths)} 成功")
    print(f"{'='*60}\n")


if __name__ == '__main__':
    main()