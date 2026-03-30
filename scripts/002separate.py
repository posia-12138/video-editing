#!/usr/bin/env python3
"""
音频分离脚本：将音频/视频分离为「人声+音效」和「背景音乐」两个文件
使用两个模型 ensemble 提升分离质量

用法：
  conda run -n msst python separate.py <输入文件> [输出目录]
  或者先激活环境：conda activate msst，然后 python separate.py <输入文件>

输出：
  vocals.wav      = 人声 + 音效（去除背景音乐）

示例：
  conda run -n msst python separate.py 录屏2026-03-16.mov
  conda run -n msst python separate.py 1.mp4 my_output
  conda run -n msst python separate.py ./videos/ ./output/
"""
import os
import sys
import shutil
import tempfile
import argparse
import subprocess

# 检查是否在正确的环境中运行
try:
    import torch
    import librosa
    import soundfile as sf
except ImportError as e:
    print(f"错误: 缺少依赖模块 {e.name}")
    print("\n请在 msst 环境中运行此脚本:")
    print("  conda activate msst")
    print("  python separate.py <输入文件>")
    print("\n或者使用:")
    print("  conda run -n msst python separate.py <输入文件>")
    sys.exit(1)

import numpy as np

MSST_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "libs", "Music-Source-Separation-Training-GUI")
sys.path.insert(0, MSST_DIR)

from utils.settings import get_model_from_config
from utils.model_utils import demix

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".webm", ".m4v"}
AUDIO_EXTS = {".wav", ".mp3", ".flac", ".aac", ".ogg", ".m4a"}

MODELS = [
    {
        "name": "vocalsandeffects",
        "type": "mel_band_roformer",
        "config": os.path.join(MSST_DIR, "configs", "config_vocalsandeffects_mel_band_roformer.yaml"),
        "ckpt": os.path.join(MSST_DIR, "pretrain", "model_mel_band_roformer_ep_3_sdr_14.8359.ckpt"),
        "key": "model_state_dict",
        "weight": 0.3,
    },
    {
        "name": "kim",
        "type": "mel_band_roformer",
        "config": os.path.join(MSST_DIR, "configs", "config_vocals_mel_band_roformer_kim.yaml"),
        "ckpt": os.path.join(MSST_DIR, "pretrain", "MelBandRoformer_kim.ckpt"),
        "key": None,
        "weight": 0.7,
    },
]


def to_wav(input_path: str, output_wav: str):
    cmd = ["ffmpeg", "-y", "-i", input_path, "-ar", "44100", "-ac", "2", "-vn", output_wav]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 转换失败:\n{result.stderr}")


def load_models():
    models = []
    for m in MODELS:
        print(f"  加载 {m['name']}...")
        model, config = get_model_from_config(m["type"], m["config"])
        ckpt = torch.load(m["ckpt"], map_location="cpu", weights_only=False)
        sd = ckpt[m["key"]] if m["key"] else ckpt
        model.load_state_dict(sd)
        model = model.cuda()
        model.eval()
        models.append((model, config, m))
    return models


def separate_file(models, wav_path: str, output_dir: str, original_path: str = None, no_subdir: bool = False):
    """
    分离音频文件

    Args:
        models: 模型列表
        wav_path: wav 文件路径
        output_dir: 输出根目录
        original_path: 原始文件路径（用于保持目录结构）
        no_subdir: 是否禁用子目录创建，直接输出到 output_dir
    """
    # 如果禁用子目录，直接使用 output_dir
    if no_subdir:
        output_name = os.path.splitext(os.path.basename(wav_path))[0]
        out_folder = output_dir
    # 如果提供了原始路径，保持相同的目录结构
    elif original_path:
        # 获取原始文件的相对路径结构
        # 例如: assets\大明：太子朱标1-7\大明：太子朱标1\1.mp4
        # 提取: 大明：太子朱标1-7\大明：太子朱标1
        parent_dir = os.path.dirname(original_path)
        # 获取倒数第二级目录名（大明：太子朱标1-7）
        grandparent = os.path.basename(os.path.dirname(parent_dir))
        # 获取上级目录名（大明：太子朱标1）
        parent_name = os.path.basename(parent_dir)

        # 构建输出路径: output\大明：太子朱标1-7\
        out_folder = os.path.join(output_dir, grandparent)
        output_name = parent_name
    else:
        output_name = os.path.splitext(os.path.basename(wav_path))[0]
        out_folder = output_dir
    
    os.makedirs(out_folder, exist_ok=True)

    mix, sr = librosa.load(wav_path, sr=44100, mono=False)
    print(f"  音频: {os.path.basename(wav_path)}  时长={mix.shape[-1]/sr:.1f}s")

    vocals_list, weights = [], []
    for model, config, m_cfg in models:
        vocals = demix(config, model, mix, torch.device("cuda:0"),
                       model_type=m_cfg["type"], pbar=False)["vocals"]
        vocals_list.append(vocals)
        weights.append(m_cfg["weight"])

    total_w = sum(weights)
    final_vocals = sum(v * w for v, w in zip(vocals_list, weights)) / total_w

    output_path = os.path.join(out_folder, f"{output_name}.wav")
    sf.write(output_path, final_vocals.T, sr, subtype="FLOAT")

    rms_v = 20 * np.log10(np.sqrt(np.mean(final_vocals**2)) + 1e-9)
    print(f"  {output_name}.wav: {rms_v:.1f} dB")
    print(f"  输出: {output_path}")


def collect_wav_files(input_path: str, tmp_dir: str):
    """
    收集输入路径下所有音视频文件，视频转 wav，返回 (wav_path, original_path) 列表
    
    Returns:
        list of tuple: [(wav_path, original_path), ...]
    """
    if os.path.isfile(input_path):
        files = [input_path]
    else:
        files = [
            os.path.join(input_path, f) for f in os.listdir(input_path)
            if os.path.splitext(f)[1].lower() in VIDEO_EXTS | AUDIO_EXTS
        ]
        if not files:
            raise ValueError(f"目录中没有找到音视频文件: {input_path}")

    wav_files = []
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext in VIDEO_EXTS:
            base = os.path.splitext(os.path.basename(f))[0]
            out_wav = os.path.join(tmp_dir, base + ".wav")
            print(f"转换: {os.path.basename(f)} -> wav")
            to_wav(f, out_wav)
            wav_files.append((out_wav, f))  # 保存原始路径
        else:
            wav_files.append((f, f))  # 音频文件也保存原始路径
    return wav_files


def main():
    parser = argparse.ArgumentParser(description="音频分离：人声+音效（去除背景音乐）")
    parser.add_argument("input", help="输入文件（mp4/mov/wav等）或目录")
    parser.add_argument("output", nargs="?", default="output", help="输出目录（默认 output）")
    parser.add_argument("--no-subdir", action="store_true", help="不创建子目录，直接输出到指定目录")
    args = parser.parse_args()

    input_path = os.path.abspath(args.input)
    output_dir = os.path.abspath(args.output)
    os.makedirs(output_dir, exist_ok=True)

    tmp_dir = tempfile.mkdtemp(prefix="msst_")
    try:
        wav_files = collect_wav_files(input_path, tmp_dir)

        print("\n加载模型...")
        models = load_models()
        print(f"模型加载完成，开始处理 {len(wav_files)} 个文件\n")
        print("  输出文件名 = 原始文件的上级目录名\n")

        for wav_path, original_path in wav_files:
            separate_file(models, wav_path, output_dir, original_path, no_subdir=args.no_subdir)

        print(f"\n完成！结果保存在: {output_dir}/")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
