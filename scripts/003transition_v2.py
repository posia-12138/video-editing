#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频拼接脚本 v2 — 带场景检测的智能淡入淡出转场

在相邻片段衔接处用 scdet 检测是否有大场景切换；
检测到切换则自动在该衔接处加黑幕淡出+淡入转场。
同时输出 {output_stem}_clips.json，供 select_music.py
在剪映轨道上摆放独立片段。
"""

import sys
import os
import subprocess
import json
import re
import math
import tempfile
from pathlib import Path

if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')


def natural_sort_key(filename):
    parts = re.split(r'(\d+)', str(filename))
    return [int(p) if p.isdigit() else p.lower() for p in parts]


def get_duration(video_path: Path) -> float:
    """用 ffprobe 获取视频时长（秒）"""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True, timeout=30,
            encoding='utf-8', errors='replace'
        )
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def get_avg_color_at(clip: Path, seek_time: float) -> tuple:
    """在指定时间点取一帧，缩放到 1x1 像素，返回平均 RGB。"""
    cmd = [
        "ffmpeg", "-ss", str(seek_time), "-i", str(clip),
        "-vframes", "1",
        "-vf", "scale=1:1:flags=area,format=rgb24",
        "-f", "rawvideo", "pipe:1",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=15)
        if len(r.stdout) >= 3:
            return (r.stdout[0], r.stdout[1], r.stdout[2])
    except Exception:
        pass
    return None


def get_boundary_color(clip: Path, duration: float, side: str, n_frames: int = 3) -> tuple:
    """
    采样片段边界处多帧后取均值，用于判断两段视频衔接处是否存在视觉跳切。
    side='tail'：取最后 n_frames 帧（各帧均匀分布在最后 10% 时长内）
    side='head'：取最前 n_frames 帧（各帧均匀分布在最前 10% 时长内）
    返回平均 RGB 元组，采样失败则返回 None。
    """
    FRAME_DUR = 1 / 30
    window = min(duration * 0.10, 1.0)  # 采样窗口：最后/最前 10%，最多 1 秒
    colors = []
    for k in range(n_frames):
        offset = window * (k + 1) / (n_frames + 1)
        if side == 'tail':
            t = max(0.0, duration - window + offset)
        else:
            t = offset
        c = get_avg_color_at(clip, t)
        if c:
            colors.append(c)
    if not colors:
        return None
    return tuple(int(sum(c[ch] for c in colors) / len(colors)) for ch in range(3))


def color_distance(ca: tuple, cb: tuple) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(ca, cb)))


def rgb_to_hsv(r: int, g: int, b: int) -> tuple:
    """RGB (0-255) → (h: 0-360, s: 0-1, v: 0-1)"""
    r_, g_, b_ = r / 255, g / 255, b / 255
    maxc = max(r_, g_, b_)
    minc = min(r_, g_, b_)
    v = maxc
    if maxc == minc:
        return 0.0, 0.0, v
    s = (maxc - minc) / maxc
    rc = (maxc - r_) / (maxc - minc)
    gc = (maxc - g_) / (maxc - minc)
    bc = (maxc - b_) / (maxc - minc)
    if r_ == maxc:
        h = bc - gc
    elif g_ == maxc:
        h = 2.0 + rc - bc
    else:
        h = 4.0 + gc - rc
    h = (h / 6.0) % 1.0
    return h * 360, s, v


def classify_color(r: int, g: int, b: int) -> dict:
    """将平均 RGB 转为可读色彩描述（色温/亮度）。
    注：短剧整体饱和度偏低，饱和度不作为情绪信号，仅保留色温和亮度。"""
    h, s, v = rgb_to_hsv(r, g, b)
    # 色温（低饱和度时色温意义不大，归为中性）
    if s < 0.12:
        warmth = "中性色调"
    elif h < 60 or h > 300:
        warmth = "暖色调"   # 红/橙/黄/品红
    elif 150 <= h <= 270:
        warmth = "冷色调"   # 蓝/青
    elif 60 <= h < 150:
        warmth = "偏绿冷调"
    else:
        warmth = "中性色调"
    # 亮度（短剧普遍偏暗，阈值适当下调）
    if v > 0.65:
        brightness = "画面偏亮"
    elif v < 0.30:
        brightness = "画面较暗"
    else:
        brightness = "亮度正常"
    return {
        "warmth": warmth,
        "brightness": brightness,
        "avg_rgb": [r, g, b],
    }


def get_color_profile(clip: Path, duration: float) -> dict:
    """采样片段 25%/50%/75% 三帧，平均后分类色彩特征。"""
    samples = []
    for pct in [0.25, 0.50, 0.75]:
        c = get_avg_color_at(clip, duration * pct)
        if c:
            samples.append(c)
    if not samples:
        return {}
    avg_r = int(sum(s[0] for s in samples) / len(samples))
    avg_g = int(sum(s[1] for s in samples) / len(samples))
    avg_b = int(sum(s[2] for s in samples) / len(samples))
    return classify_color(avg_r, avg_g, avg_b)


def find_script_for_folder(folder: Path) -> Path | None:
    """
    在 folder 的父目录下的兄弟目录里找 Episode-XX.md。
    例如 assets/双骄/双娇-02 → 搜索 assets/双骄/*/Episode-02.md
    """
    m = re.search(r'(\d+)$', folder.name)
    if not m:
        return None
    ep_file = f"Episode-{m.group(1)}.md"
    for sibling in folder.parent.iterdir():
        if sibling.is_dir() and sibling != folder:
            candidate = sibling / ep_file
            if candidate.exists():
                return candidate
    return None


def parse_script_scenes(script_path: Path) -> list:
    """
    解析剧本，返回 [(scene_id, location, line_count), ...]。
    场景标记格式：行首形如 "1-1"、"2-3" 的数字-数字行。
    """
    lines = script_path.read_text(encoding='utf-8').splitlines()
    scenes = []
    current = None
    current_loc = ''
    start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r'^\d+-\d+$', stripped):
            if current is not None:
                scenes.append((current, current_loc, i - start))
            current = stripped
            current_loc = ''
            start = i
        elif current and stripped.startswith('地点') and not current_loc:
            current_loc = stripped[2:].strip().split()[0]  # 取第一个词
    if current is not None:
        scenes.append((current, current_loc, len(lines) - start))
    return scenes


def select_junctions_by_script(scenes: list, junction_infos: list,
                                video_dur_s: float,
                                window_pct: float = 0.20,
                                min_dist: float = 70.0) -> set:
    """
    用剧本场景行数比例估算各场景切换的时间位置，
    用加权评分 = 颜色距离 × 位置接近度 选出最佳衔接点。

    位置接近度 = max(0, 1 - dist_from_target / window_s)
    越接近预测位置、颜色差异越大，得分越高。

    min_dist: 边界帧颜色距离最低门槛。低于此值说明两段视频在衔接处视觉
    上是连续的，不是真正的场景切换，即使剧本预测此处有转场也跳过。

    junction_infos: [(index, dist, time_s), ...]
    """
    total_lines = sum(c for _, _, c in scenes)
    selected = set()
    already_used = set()
    cumulative = 0
    for si, (scene_id, loc, count) in enumerate(scenes[:-1]):
        cumulative += count
        expected_s = (cumulative / total_lines) * video_dur_s
        window_s = window_pct * video_dur_s

        # 先过 min_dist 门槛，再从满足条件的候选中选最靠近预测时间点的
        candidates = [
            (idx, dist, t) for idx, dist, t in junction_infos
            if idx not in already_used
            and dist >= min_dist
            and abs(t - expected_s) <= window_s
        ]

        next_scene = scenes[si + 1]
        if candidates:
            best_idx, best_dist, best_t = min(candidates, key=lambda x: abs(x[2] - expected_s))
            print(f"  场景 {scene_id}({loc}) → {next_scene[0]}({next_scene[1]})"
                  f"  预测 {expected_s:.1f}s"
                  f"  → 衔接点 {best_idx+1}→{best_idx+2} @ {best_t:.1f}s"
                  f"  (dist={best_dist:.1f})")
            selected.add(best_idx)
            already_used.add(best_idx)
        else:
            # 窗口内所有候选均低于 min_dist，打印 dist 最高的供参考
            in_window = [(idx, dist, t) for idx, dist, t in junction_infos
                         if idx not in already_used and abs(t - expected_s) <= window_s]
            if in_window:
                top = max(in_window, key=lambda x: x[1])
                print(f"  场景 {scene_id}({loc}) → {next_scene[0]}({next_scene[1]})"
                      f"  预测 {expected_s:.1f}s"
                      f"  → 窗口内最高 dist={top[1]:.1f} < {min_dist}，视觉连续，跳过")
            else:
                print(f"  场景切换预测 {expected_s:.1f}s  → 窗口内无候选衔接点，跳过")
    return selected


def main():
    import argparse

    parser = argparse.ArgumentParser(description='视频拼接器 v2 — 场景检测淡入淡出转场')
    parser.add_argument('--folder', '-f', required=True, help='视频文件夹路径')
    parser.add_argument('-o', '--output', required=True, help='输出文件路径')
    parser.add_argument('--bgm', help='背景音乐文件路径（flac/mp3/wav 等）')
    parser.add_argument('--bgm-volume', type=float, default=0.3,
                        help='背景音乐音量（0.0-1.0，默认 0.3）')
    parser.add_argument('--fade-duration', type=float, default=1.0,
                        help='淡出/淡入时长（秒，默认 1.0）')
    parser.add_argument('--threshold', type=float, default=25.0,
                        help='场景切换检测阈值：结尾帧与开头帧平均颜色的 RGB 欧氏距离，超过则加转场（默认 25，最大约 442）')
    parser.add_argument('--no-detect', action='store_true',
                        help='跳过场景检测，对所有衔接处加淡入淡出')
    parser.add_argument('--junctions', type=str, default='',
                        help='手动指定场景切换点，格式：片段编号（1起），逗号分隔。'
                             '例如 --junctions 3,7 表示在3→4和7→8之间加转场。'
                             '指定后跳过自动检测。')
    parser.add_argument('--script', type=str, default='',
                        help='剧本文件路径（.md）。不指定则自动在兄弟目录下寻找 Episode-XX.md。')

    args = parser.parse_args()

    folder_path = Path(args.folder)
    if not folder_path.exists() or not folder_path.is_dir():
        print(f"❌ 文件夹不存在: {args.folder}")
        return 1

    video_paths = sorted(folder_path.glob('*.mp4'), key=natural_sort_key)
    if not video_paths:
        print("❌ 没有找到 mp4 文件")
        return 1

    print(f"\n找到 {len(video_paths)} 个视频文件:")
    for i, v in enumerate(video_paths, 1):
        print(f"  {i}. {v.name}")

    n = len(video_paths)

    # ── 0. 提前获取时长（场景检测需要用来估算衔接点时间位置）──────────────────────
    durations = [get_duration(v) for v in video_paths]

    # ── 1. 场景切换检测 ────────────────────────────────────────────────────────
    fade_junctions = set()  # 需要加淡入淡出的衔接点（i 表示 clip[i] → clip[i+1]）

    if n > 1:
        if args.junctions.strip():
            # 手动指定模式：解析 "3,7" → 内部 0-indexed 的 {2, 6}
            manual = set()
            for tok in args.junctions.split(','):
                tok = tok.strip()
                if tok.isdigit():
                    idx = int(tok) - 1  # 用户用 1-based，内部用 0-based
                    if 0 <= idx < n - 1:
                        manual.add(idx)
                    else:
                        print(f"  [警告] --junctions 中的 {tok} 超出范围（片段总数 {n}），忽略")
            fade_junctions = manual
            names = ', '.join(f"{video_paths[i].name}→{video_paths[i+1].name}"
                              for i in sorted(fade_junctions))
            print(f"\n📌 手动指定转场衔接点: {names or '（无）'}")
        elif args.no_detect:
            print(f"\n⏭  跳过场景检测，对全部衔接处加淡入淡出")
            fade_junctions = set(range(n - 1))
        else:
            # ── 颜色检测：计算所有衔接点的颜色距离 ──────────────────────────────
            print(f"\n🔍 颜色检测（各片段中间帧对比）...")
            junction_times = []  # 各衔接点在视频中的时间位置（秒）
            t = 0.0
            for d in durations[:-1]:
                t += d
                junction_times.append(t)

            junction_infos = []  # [(index, dist, time_s), ...]
            for i in range(n - 1):
                dur_a, dur_b = durations[i], durations[i + 1]
                ca = get_boundary_color(video_paths[i], dur_a, side='tail')
                cb = get_boundary_color(video_paths[i + 1], dur_b, side='head')
                dist = color_distance(ca, cb) if (ca and cb) else 0.0
                junction_infos.append((i, dist, junction_times[i]))
                print(f"  {video_paths[i].name} → {video_paths[i+1].name}:  dist={dist:.1f}")

            # ── 脚本辅助：用场景行数比例定位切换点，在窗口内取颜色距离最大者 ─────
            script_path = (Path(args.script) if getattr(args, 'script', None)
                           else find_script_for_folder(folder_path))
            video_dur_s = sum(durations)

            if script_path and script_path.exists():
                scenes = parse_script_scenes(script_path)
                n_scenes = len(scenes)
                print(f"\n📖 剧本辅助检测: {script_path.name}  共 {n_scenes} 个场景")
                if n_scenes > 1:
                    fade_junctions = select_junctions_by_script(
                        scenes, junction_infos, video_dur_s)
                else:
                    print(f"  剧本只有 1 个场景，无需转场")
            else:
                # 无剧本：回退到阈值模式
                print(f"\n  [无剧本] 使用颜色距离阈值={args.threshold}")
                for i, dist, _ in junction_infos:
                    if dist > args.threshold:
                        print(f"  ✅ {video_paths[i].name}→{video_paths[i+1].name} 加转场")
                        fade_junctions.add(i)

    # ── 2. 生成 clips manifest ──────────────────────────────────────────────────

    output_path = Path(args.output)
    manifest_path = output_path.parent / f"{output_path.stem}_clips.json"

    FRAME_DUR = 1 / 30  # 统一 30fps，1帧时长

    clips_manifest = []
    current_time = 0.0
    for i, (vp, dur) in enumerate(zip(video_paths, durations)):
        # 场景切换后的首片段多去 10 帧（防止黑场后仍残留前一场景内容）
        # 普通连续片段去 1 帧（视频生成工具复制的重复首帧）
        if i == 0:
            trim_frames = 0
        elif (i - 1) in fade_junctions:
            trim_frames = 10
        else:
            trim_frames = 1
        effective_dur = dur - trim_frames * FRAME_DUR
        clips_manifest.append({
            "file":         vp.name,
            "start_s":      round(current_time, 6),
            "duration_s":   round(effective_dur, 6),
            "has_fade_after": i in fade_junctions,
            "trim_frames":  trim_frames,
        })
        current_time += effective_dur

    # ── 2b. 色彩分析（仅分析转场前后片段，减少 ffmpeg 调用次数）──────────────────
    if fade_junctions:
        print(f"\n🎨 色彩分析（转场前后片段）...")
        color_needed = set()
        for j in fade_junctions:
            color_needed.add(j)      # 转场前片段
            color_needed.add(j + 1)  # 转场后片段
        for i, (vp, dur) in enumerate(zip(video_paths, durations)):
            if i in color_needed:
                profile = get_color_profile(vp, dur)
                clips_manifest[i]["color_profile"] = profile
                if profile:
                    print(f"   {vp.name}: {profile['warmth']} / {profile['brightness']}")

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(clips_manifest, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    print(f"\n📄 Clips manifest 已保存: {manifest_path.name}")
    for c in clips_manifest:
        fade_flag = " [→转场]" if c["has_fade_after"] else ""
        print(f"   {c['file']}  {c['start_s']:.1f}s + {c['duration_s']:.1f}s{fade_flag}")

    # ── 3. 构建 ffmpeg filter_complex ─────────────────────────────────────────
    print(f"\n🔧 统一视频参数（1920x1080 @ 30fps）...")

    fd = args.fade_duration
    filter_parts = []

    for i, (vp, dur) in enumerate(zip(video_paths, durations)):
        clip_info = clips_manifest[i]
        eff_dur = clip_info["duration_s"]

        base = (
            f"[{i}:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
            f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black,"
            f"fps=30,setsar=1,format=yuv420p"
        )
        tf = clip_info["trim_frames"]
        vfilters = []
        if tf > 0:
            vfilters.append(f"trim=start_frame={tf},setpts=PTS-STARTPTS")
        if (i - 1) in fade_junctions:          # 本片段开头淡入
            vfilters.append(f"fade=t=in:st=0:d={fd}")
        if i in fade_junctions:                 # 本片段结尾淡出
            st = max(0.0, eff_dur - fd)
            vfilters.append(f"fade=t=out:st={st:.3f}:d={fd}")

        if vfilters:
            filter_parts.append(f"{base},{','.join(vfilters)}[v{i}]")
        else:
            filter_parts.append(f"{base}[v{i}]")

        # 音频：同步去掉对应帧时长
        if tf > 0:
            filter_parts.append(
                f"[{i}:a]atrim=start={tf * FRAME_DUR:.6f},asetpts=PTS-STARTPTS[a{i}]"
            )

    # 视频 concat
    filter_parts.append(
        ''.join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vout]"
    )
    # 音频 concat（有 trim 的片段用处理后的 [a{i}]，其余用原始流）
    audio_inputs = ''.join(
        f"[a{i}]" if clips_manifest[i]["trim_frames"] > 0 else f"[{i}:a]"
        for i in range(n)
    )
    filter_parts.append(f"{audio_inputs}concat=n={n}:v=0:a=1[aout]")

    filter_complex = ';'.join(filter_parts)
    input_args = []
    for vp in video_paths:
        input_args.extend(['-i', str(vp)])

    # ── 4. 执行 ffmpeg 拼接 ────────────────────────────────────────────────────
    print(f"\n🎬 拼接视频...")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if args.bgm and Path(args.bgm).exists():
        print(f"🎵 添加背景音乐: {Path(args.bgm).name}  音量={args.bgm_volume}")

        temp_output = str(output_path.parent / f"temp_{output_path.name}")

        cmd = [
            'ffmpeg', '-y',
            *input_args,
            '-filter_complex', filter_complex,
            '-map', '[vout]', '-map', '[aout]',
            '-c:v', 'libx264', '-preset', 'medium', '-crf', '23',
            '-c:a', 'aac', '-b:a', '192k',
            temp_output
        ]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            print(f"❌ 视频拼接失败")
            print(result.stderr.decode('utf-8', errors='ignore'))
            return 1

        cmd = [
            'ffmpeg', '-y',
            '-i', temp_output,
            '-i', str(args.bgm),
            '-filter_complex',
            f'[0:a][1:a]amix=inputs=2:duration=first:weights=1 {args.bgm_volume}[aout]',
            '-map', '0:v', '-map', '[aout]',
            '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
            str(output_path)
        ]
        result = subprocess.run(cmd, capture_output=True)

        if Path(temp_output).exists():
            Path(temp_output).unlink()

        if result.returncode != 0:
            print(f"❌ 音频混合失败")
            print(result.stderr.decode('utf-8', errors='ignore'))
            return 1
    else:
        cmd = [
            'ffmpeg', '-y',
            *input_args,
            '-filter_complex', filter_complex,
            '-map', '[vout]', '-map', '[aout]',
            '-c:v', 'libx264', '-preset', 'medium', '-crf', '23',
            '-c:a', 'aac', '-b:a', '192k',
            str(output_path)
        ]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            print(f"❌ 拼接失败")
            print(result.stderr.decode('utf-8', errors='ignore'))
            return 1

    if fade_junctions:
        print(f"\n✅ 拼接完成！在 {len(fade_junctions)} 处衔接点加了淡入淡出转场")
    else:
        print(f"\n✅ 拼接完成！（无大场景切换，无额外转场）")
    print(f"📁 输出文件: {output_path}")

    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n⚠️  用户中断")
        sys.exit(1)
