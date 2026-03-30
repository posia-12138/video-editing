#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
简单视频拼接脚本
使用filter_complex统一分辨率和帧率后拼接，避免画面冻结问题
"""

import sys
import os
import subprocess
from pathlib import Path

# 设置UTF-8编码输出（Windows兼容）
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')


def natural_sort_key(filename):
    """自然排序键函数"""
    import re
    parts = re.split(r'(\d+)', str(filename))
    return [int(part) if part.isdigit() else part.lower() for part in parts]


def main():
    import argparse

    parser = argparse.ArgumentParser(description='简单视频拼接器 - 统一参数后拼接')
    parser.add_argument('--folder', '-f', required=True, help='视频文件夹路径')
    parser.add_argument('-o', '--output', required=True, help='输出文件路径')
    parser.add_argument('--bgm', help='背景音乐文件路径（flac/mp3/wav等）')
    parser.add_argument('--bgm-volume', type=float, default=0.3, help='背景音乐音量（0.0-1.0，默认0.3）')

    args = parser.parse_args()

    # 获取视频文件列表
    folder_path = Path(args.folder)
    if not folder_path.exists() or not folder_path.is_dir():
        print(f"❌ 文件夹不存在: {args.folder}")
        return 1

    video_paths = sorted(folder_path.glob('*.mp4'), key=natural_sort_key)

    if len(video_paths) < 1:
        print("❌ 至少需要1个视频文件")
        return 1

    print(f"\n找到 {len(video_paths)} 个视频文件:")
    for i, v in enumerate(video_paths, 1):
        print(f"  {i}. {v.name}")

    print(f"\n🔧 统一视频参数（1920x1080 @ 30fps）...")

    # 构建filter_complex：统一所有视频的分辨率、帧率、像素格式
    # 使用scale+pad确保所有视频都是1920x1080，保持宽高比，黑边填充
    filter_parts = []
    for i in range(len(video_paths)):
        # scale: 缩放到1920x1080内，保持宽高比
        # pad: 填充黑边到1920x1080
        # fps: 统一帧率为30fps
        # setsar: 设置像素宽高比为1:1
        # format: 统一像素格式为yuv420p
        filter_parts.append(
            f"[{i}:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
            f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black,"
            f"fps=30,setsar=1,format=yuv420p[v{i}]"
        )

    # 拼接所有视频流
    video_concat_inputs = ''.join(f"[v{i}]" for i in range(len(video_paths)))
    filter_parts.append(f"{video_concat_inputs}concat=n={len(video_paths)}:v=1:a=0[vout]")

    # 拼接所有音频流
    audio_concat_inputs = ''.join(f"[{i}:a]" for i in range(len(video_paths)))
    filter_parts.append(f"{audio_concat_inputs}concat=n={len(video_paths)}:v=0:a=1[aout]")

    filter_complex = ';'.join(filter_parts)

    # 构建输入参数
    input_args = []
    for vf in video_paths:
        input_args.extend(['-i', str(vf)])

    print(f"\n🎬 拼接视频...")

    if args.bgm and Path(args.bgm).exists():
        print(f"🎵 添加背景音乐: {args.bgm}")
        print(f"   音量: {args.bgm_volume}")

        # 先拼接视频和音频
        temp_output = str(Path(args.output).parent / f"temp_{Path(args.output).name}")

        cmd = [
            'ffmpeg', '-y',
            *input_args,
            '-filter_complex', filter_complex,
            '-map', '[vout]',
            '-map', '[aout]',
            '-c:v', 'libx264',
            '-preset', 'medium',
            '-crf', '23',
            '-c:a', 'aac',
            '-b:a', '192k',
            temp_output
        ]

        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            print(f"❌ 视频拼接失败")
            print(result.stderr.decode('utf-8', errors='ignore'))
            return 1

        # 混合背景音乐
        cmd = [
            'ffmpeg', '-y',
            '-i', temp_output,
            '-i', str(args.bgm),
            '-filter_complex', f'[0:a][1:a]amix=inputs=2:duration=first:weights=1 {args.bgm_volume}[aout]',
            '-map', '0:v',
            '-map', '[aout]',
            '-c:v', 'copy',
            '-c:a', 'aac',
            '-b:a', '192k',
            str(args.output)
        ]

        result = subprocess.run(cmd, capture_output=True)

        # 删除临时文件
        if Path(temp_output).exists():
            Path(temp_output).unlink()

        if result.returncode != 0:
            print(f"❌ 音频混合失败")
            print(result.stderr.decode('utf-8', errors='ignore'))
            return 1
    else:
        # 直接拼接（包含音频和视频）
        cmd = [
            'ffmpeg', '-y',
            *input_args,
            '-filter_complex', filter_complex,
            '-map', '[vout]',
            '-map', '[aout]',
            '-c:v', 'libx264',
            '-preset', 'medium',
            '-crf', '23',
            '-c:a', 'aac',
            '-b:a', '192k',
            str(args.output)
        ]

        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            print(f"❌ 拼接失败")
            print(result.stderr.decode('utf-8', errors='ignore'))
            return 1

    print(f"\n✅ 拼接完成！")
    print(f"📁 输出文件: {args.output}")

    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断")
        sys.exit(1)
