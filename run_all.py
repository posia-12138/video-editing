#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
完整剪辑流程自动化脚本
调用四个独立脚本完成完整的视频编辑流程
"""

import os
import sys
import json
import subprocess
import re
from pathlib import Path

# 设置 Windows 控制台编码为 UTF-8
if sys.platform == 'win32':
    try:
        import codecs
        sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
        sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')
    except:
        pass


def load_config(config_path="config-example.json"):
    """加载配置文件"""
    if not Path(config_path).exists():
        print(f"❌ 配置文件不存在: {config_path}")
        sys.exit(1)
    
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def find_conda_env_python(env_name):
    """自动查找 conda 环境的 Python 路径"""
    try:
        # 获取 conda 信息
        result = subprocess.run(
            ['conda', 'info', '--envs'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore'
        )
        
        if result.returncode != 0:
            print(f"❌ 无法获取 conda 环境信息")
            return None
        
        # 解析输出找到环境路径
        for line in result.stdout.split('\n'):
            if env_name in line and not line.startswith('#'):
                parts = line.split()
                for part in parts:
                    if os.path.exists(part):
                        env_path = Path(part)
                        # 查找 Python 可执行文件
                        if sys.platform == 'win32':
                            python_exe = env_path / 'python.exe'
                        else:
                            python_exe = env_path / 'bin' / 'python'
                        
                        if python_exe.exists():
                            print(f"✅ 找到 conda 环境 {env_name}: {python_exe}")
                            return python_exe
        
        print(f"❌ 未找到 conda 环境: {env_name}")
        return None
    except Exception as e:
        print(f"❌ 查找 conda 环境时出错: {e}")
        return None


def natural_sort_key(path):
    """自然排序键函数"""
    parts = re.split(r'(\d+)', str(path) if isinstance(path, Path) else path)
    return [int(part) if part.isdigit() else part.lower() for part in parts]


def find_videos(folder):
    """查找文件夹中的所有视频文件（按数字排序）"""
    videos = list(folder.glob("*.mp4"))
    return sorted(videos, key=natural_sort_key)


def find_audio(folder):
    """查找背景音乐文件"""
    for ext in ['.flac', '.mp3', '.wav', '.m4a']:
        audio_files = list(folder.glob(f"*{ext}"))
        if audio_files:
            return audio_files[0]
    return None


def parse_srt_time(time_str):
    """解析SRT时间格式 (HH:MM:SS,mmm) 为秒数"""
    import re
    m = re.match(r'(\d+):(\d+):(\d+),(\d+)', time_str)
    if m:
        h, m, s, ms = map(int, m.groups())
        return h * 3600 + m * 60 + s + ms / 1000.0
    return 0.0


def format_srt_time(seconds):
    """将秒数格式化为SRT时间格式 (HH:MM:SS,mmm)"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def get_video_duration(video_path):
    """获取视频时长（秒）"""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', str(video_path)],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore'
        )
        if result.returncode == 0 and result.stdout:
            import json
            data = json.loads(result.stdout)
            duration = float(data['format']['duration'])
            return duration
    except Exception as e:
        print(f"  ⚠️  无法获取视频时长: {video_path.name}, 错误: {e}")
    return 0.0


def merge_and_adjust_subtitles(srt_files, output_srt, video_folder, config_path=None):
    """
    合并多个SRT文件并调整时间轴
    考虑转场效果导致的时间重叠
    """
    print(f"合并 {len(srt_files)} 个字幕文件...")

    # 获取所有视频的时长
    video_files = sorted(video_folder.glob("*.mp4"), key=lambda p: natural_sort_key(p.name))
    durations = []
    for vf in video_files:
        dur = get_video_duration(vf)
        durations.append(dur)
        print(f"  {vf.name}: {dur:.2f}s")

    # 读取转场配置
    if config_path is None:
        config_path = Path('config.json') if Path('config.json').exists() else Path(__file__).parent / "config-example.json"
    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)

    use_random_transitions = config.get('transition', {}).get('use_random_transitions', True)

    # 估算转场时长（简化处理，假设平均0.6秒）
    avg_transition_duration = 0.6

    # 合并字幕
    merged_subtitles = []
    subtitle_index = 1
    time_offset = 0.0

    for i, srt_file in enumerate(srt_files):
        if not srt_file.exists():
            print(f"  ⚠️  字幕文件不存在: {srt_file}")
            continue

        print(f"  处理: {srt_file.name}, 时间偏移: {time_offset:.2f}s")

        # 读取SRT文件
        with open(srt_file, 'r', encoding='utf-8') as f:
            content = f.read()

        # 解析SRT
        import re
        pattern = r'(\d+)\n(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})\n(.*?)(?=\n\n|\Z)'
        matches = re.findall(pattern, content, re.DOTALL)

        for match in matches:
            _, start_time, end_time, text = match
            start_sec = parse_srt_time(start_time) + time_offset
            end_sec = parse_srt_time(end_time) + time_offset

            merged_subtitles.append({
                'index': subtitle_index,
                'start': start_sec,
                'end': end_sec,
                'text': text.strip()
            })
            subtitle_index += 1

        # 更新时间偏移：当前视频时长 - 转场时长
        if i < len(durations):
            if i < len(durations) - 1:
                # 不是最后一个视频，减去转场时长
                time_offset += durations[i] - avg_transition_duration
            else:
                # 最后一个视频，不减去转场时长
                time_offset += durations[i]

    # 写入合并后的SRT
    with open(output_srt, 'w', encoding='utf-8') as f:
        for sub in merged_subtitles:
            f.write(f"{sub['index']}\n")
            f.write(f"{format_srt_time(sub['start'])} --> {format_srt_time(sub['end'])}\n")
            f.write(f"{sub['text']}\n\n")

    print(f"✅ 合并完成，共 {len(merged_subtitles)} 条字幕")
    print(f"📁 输出: {output_srt}")


def burn_subtitle_to_video(video_path, srt_path, output_path):
    """使用ffmpeg将字幕烧录到视频"""
    print(f"\n烧录字幕到视频...")

    # Windows下需要转义路径
    srt_str = str(srt_path).replace('\\', '/').replace(':', '\\:')

    style = (
        'FontName=Microsoft YaHei,'
        'FontSize=18,'
        'PrimaryColour=&H00FFFFFF,'
        'OutlineColour=&H00000000,'
        'Outline=1,'
        'Shadow=1,'
        'BackColour=&H80000000,'
        'Alignment=2'
    )

    cmd = [
        'ffmpeg', '-y',
        '-i', str(video_path),
        '-vf', f"subtitles='{srt_str}':force_style='{style}'",
        '-c:a', 'copy',
        str(output_path)
    ]

    result = subprocess.run(cmd, capture_output=True)
    if result.returncode == 0:
        print(f"✅ 字幕烧录完成")
    else:
        print(f"❌ 字幕烧录失败")
        print(result.stderr.decode('utf-8', errors='ignore'))


def replace_audio_in_video(video_path, audio_path, output_path):
    """使用 ffmpeg 替换视频的音轨"""
    cmd = [
        'ffmpeg', '-y',
        '-i', str(video_path),
        '-i', str(audio_path),
        '-c:v', 'copy',
        '-map', '0:v:0',
        '-map', '1:a:0',
        '-shortest',
        str(output_path)
    ]
    result = subprocess.run(cmd, capture_output=True)
    return result.returncode == 0


def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description='完整剪辑流程自动化脚本',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python run_all.py --folder assets/项目名/episodes/第01集
  python run_all.py --folder assets/项目名/episodes/第01集 --skip 2
        """
    )
    
    parser.add_argument('--folder', '-f', required=True, help='视频文件夹路径')
    parser.add_argument('--config', '-c', default=None, help='配置文件路径（默认优先使用 config.json，否则 config-example.json）')
    parser.add_argument('--skip', nargs='+', type=int, choices=[1, 2, 3, 4],
                       help='跳过的步骤（1=黑屏 2=分离 3=转场 4=字幕）')
    
    args = parser.parse_args()

    # 自动选择配置文件
    if args.config is None:
        if Path('config.json').exists():
            args.config = 'config.json'
        else:
            args.config = 'config-example.json'
            print(f"⚠️  未找到 config.json，使用 config-example.json（API key 为占位符，字幕步骤可能失败）")

    # 加载配置
    config = load_config(args.config)
    base_dir = Path(__file__).parent
    folder = base_dir / args.folder
    
    if not folder.exists():
        print(f"❌ 文件夹不存在: {folder}")
        sys.exit(1)
    
    # 查找视频和音乐
    original_videos = find_videos(folder)
    if not original_videos:
        # 没有直接的视频文件，检查是否有含视频的子文件夹（批量模式）
        sub_folders = sorted(
            [d for d in folder.iterdir() if d.is_dir() and find_videos(d)],
            key=natural_sort_key
        )
        if sub_folders:
            # === 批量前检查：三要素（音乐、封面、剧本） ===
            _batch_script_dir = None
            try:
                for _sub in folder.iterdir():
                    if _sub.is_dir() and '设定集' in _sub.name:
                        _batch_script_dir = _sub
                        break
            except Exception:
                pass
            if _batch_script_dir is None:
                try:
                    _assets_path = base_dir / config['paths']['assets']
                    _drama_in_assets = _assets_path / folder.name
                    if _drama_in_assets.exists():
                        for _sub in _drama_in_assets.iterdir():
                            if _sub.is_dir() and '设定集' in _sub.name:
                                _batch_script_dir = _sub
                                break
                except Exception:
                    pass

            print(f"\n[批量前检查] 找到 {len(sub_folders)} 个子文件夹，设定集: {_batch_script_dir.name if _batch_script_dir else '未找到'}")
            print(f"{'─'*60}")
            _check_missing = False
            for _sf in sub_folders:
                _bgm_ok   = find_audio(_sf) is not None
                _cover_ok = any((_sf / f"封面{ext}").exists()
                                for ext in ['.jpg', '.jpeg', '.png', '.JPG', '.JPEG', '.PNG'])
                _ep_ok = False
                if _batch_script_dir:
                    _m = re.search(r'第(\d+)集|(\d+)\s*$', _sf.name)
                    if _m:
                        _ep = int(_m.group(1) if _m.group(1) else _m.group(2))
                        for _fmt in [f"Episode-{_ep:03d}.md", f"Episode-{_ep:02d}.md", f"Episode-{_ep}.md"]:
                            if (_batch_script_dir / _fmt).exists():
                                _ep_ok = True
                                break
                _ok = lambda b: "✓" if b else "✗"
                if not (_bgm_ok and _cover_ok and _ep_ok):
                    _check_missing = True
                print(f"  {_sf.name}  音乐:{_ok(_bgm_ok)} 封面:{_ok(_cover_ok)} 剧本:{_ok(_ep_ok)}")
            print(f"{'─'*60}")
            if _check_missing:
                print("❌ 存在缺失项，请补全后重新运行")
                sys.stdout.flush()
                sys.exit(1)
            print(f"✅ 三要素齐全，开始批量处理\n")
            sys.stdout.flush()
            # === 批量前检查结束 ===

            print(f"{'='*60}")
            print(f"批量模式：在 {folder.name} 下找到 {len(sub_folders)} 个子文件夹")
            print(f"{'='*60}")
            failed = []
            for sf in sub_folders:
                print(f"\n>>> 处理子文件夹: {sf.name}")
                sub_args = [sys.executable, __file__, '--folder', str(sf.relative_to(base_dir))]
                if args.config != 'config-example.json':
                    sub_args += ['--config', args.config]
                if args.skip:
                    sub_args += ['--skip'] + [str(s) for s in args.skip]
                result = subprocess.run(sub_args, cwd=str(base_dir))
                if result.returncode != 0:
                    failed.append(sf.name)
            if failed:
                print(f"\n❌ 以下子文件夹处理失败: {failed}")
                sys.exit(1)
            else:
                print(f"\n✅ 全部 {len(sub_folders)} 个子文件夹处理完成")
                sys.exit(0)
        print(f"❌ 文件夹中没有找到视频文件")
        sys.exit(1)
    
    bgm = find_audio(folder)
    
    print(f"\n{'='*60}")
    print(f"开始处理: {folder.name}")
    print(f"{'='*60}")
    print(f"找到 {len(original_videos)} 个视频文件")
    if bgm:
        print(f"找到背景音乐: {bgm.name}")
    
    skip_steps = args.skip or []
    
    # 创建输出目录结构：保持与assets相同的目录层级
    try:
        assets_path = base_dir / config['paths']['assets']
        relative_path = folder.relative_to(assets_path)

        # transition: 转场后的视频（未烧录字幕），保持与assets相同的目录结构
        # 例如：assets/高手下山：美女请留步/高手下山，美女请留步01
        #   -> output/transition/高手下山：美女请留步/高手下山，美女请留步01.mp4
        output_004_dir = base_dir / config['paths']['output'] / "transition" / relative_path.parent
        output_004_dir.mkdir(parents=True, exist_ok=True)
        output_004 = output_004_dir / f"{folder.name}.mp4"

        # final: 字幕烧录后的最终视频，保持与assets相同的目录结构
        output_003_dir = base_dir / config['paths']['output'] / "final" / relative_path.parent
        output_003_dir.mkdir(parents=True, exist_ok=True)
        final_output = output_003_dir / f"{folder.name}.mp4"
    except ValueError:
        # 如果不在assets下，使用简化的输出结构
        output_004_dir = base_dir / config['paths']['output'] / "transition"
        output_004_dir.mkdir(parents=True, exist_ok=True)
        output_004 = output_004_dir / f"{folder.name}.mp4"

        output_003_dir = base_dir / config['paths']['output'] / "final"
        output_003_dir.mkdir(parents=True, exist_ok=True)
        final_output = output_003_dir / f"{folder.name}.mp4"
    
    # temp_output 也按照 assets 目录结构组织
    try:
        assets_path = base_dir / config['paths']['assets']
        relative_path = folder.relative_to(assets_path)
        temp_dir = base_dir / config['paths']['temp_output'] / relative_path
    except ValueError:
        temp_dir = base_dir / config['paths']['temp_output'] / folder.name

    temp_dir.mkdir(parents=True, exist_ok=True)

    # 提前推断剧本目录和集数（步骤3 BGM 和步骤4 字幕都需要）
    script_dir = None
    episode_number = None
    project_name = None
    try:
        assets_path = base_dir / config['paths']['assets']
        relative_path = folder.relative_to(assets_path)
        if len(relative_path.parts) >= 2:
            project_name = relative_path.parts[0]
            folder_name = relative_path.parts[1]
        elif len(relative_path.parts) == 1:
            folder_name = relative_path.parts[0]
            m = re.search(r'(\d+)\s*$', folder_name)
            project_name = re.sub(r'\d+\s*$', '', folder_name).strip() if m else folder_name
        else:
            project_name = None

        if project_name:
            m = re.search(r'第(\d+)集|(\d+)\s*$', relative_path.parts[-1])
            if m:
                episode_number = int(m.group(1) if m.group(1) else m.group(2))

            # 优先：在剧名目录下寻找 *设定集* 子目录（新规范）
            # 例：assets/一夜情深：霍少放肆宠/深情-设定集/Episode-01.md
            drama_dir = folder.parent
            for sub in drama_dir.iterdir():
                if sub.is_dir() and '设定集' in sub.name:
                    script_dir = sub
                    print(f"找到设定集目录: {script_dir}")
                    break

            # 回退：在 assets/script/ 下按剧名模糊匹配（旧规范）
            if not script_dir:
                script_base = base_dir / 'assets' / 'script'
                if script_base.exists():
                    for script_candidate in script_base.iterdir():
                        if not script_candidate.is_dir():
                            continue
                        dir_name = script_candidate.name
                        clean_name = re.sub(r'[_\s]*(Script|script).*$', '', dir_name, flags=re.IGNORECASE)
                        clean_project = re.sub(r'[：:，,、]', '', project_name)
                        clean_dir = re.sub(r'[：:，,、]', '', clean_name)
                        if clean_project == clean_dir or project_name in dir_name:
                            script_dir = script_candidate
                            break
    except (ValueError, Exception):
        pass

    # 回退：folder 不在 assets 下（如从 scripts/output/subtitle/ 运行）时，
    # 尝试从路径推断 project_name，然后在 assets/ 下寻找对应的设定集目录
    if project_name is None:
        # 取 folder.parent.name 作为剧名（如 "太古凌霄：唯我独尊"），folder.name 作为集数文件夹
        inferred_project = folder.parent.name
        inferred_folder  = folder.name
        if inferred_project:
            project_name = inferred_project
            m = re.search(r'第(\d+)集|(\d+)\s*$', inferred_folder)
            if m:
                episode_number = int(m.group(1) if m.group(1) else m.group(2))
            # 在 assets/<project_name>/ 下查找 *设定集* 子目录
            assets_path = base_dir / config['paths']['assets']
            assets_drama_dir = assets_path / inferred_project
            if assets_drama_dir.exists():
                for sub in assets_drama_dir.iterdir():
                    if sub.is_dir() and '设定集' in sub.name:
                        script_dir = sub
                        print(f"[回退] 找到设定集目录: {script_dir}")
                        break
            if not script_dir:
                print(f"[WARNING] 未在 assets/{inferred_project}/ 找到设定集目录，将无剧本纠错")

    # 如果文件夹里没有音乐，尝试从 JSON 剧本文件提取 aliyun_url 下载 MP3
    if not bgm and episode_number is not None:
        import json as _json, urllib.request as _urllib_req

        # 构建候选 JSON 目录列表
        json_dirs_to_try = []
        # 1. 先查 script_dir（旧规范 JSON 可能在那里）
        if script_dir:
            json_dirs_to_try.append(script_dir)
        # 2. 再查 assets/script/ 下按剧名模糊匹配的目录（JSON 文件通常在此处）
        if project_name:
            script_base = base_dir / 'assets' / 'script'
            if script_base.exists():
                for sc in sorted(script_base.iterdir()):
                    if not sc.is_dir() or sc in json_dirs_to_try:
                        continue
                    clean_name = re.sub(r'[_\s]*(Script|script).*$', '', sc.name, flags=re.IGNORECASE)
                    clean_project = re.sub(r'[：:，,、]', '', project_name)
                    clean_dir = re.sub(r'[：:，,、]', '', clean_name)
                    if clean_project == clean_dir or project_name in sc.name:
                        json_dirs_to_try.append(sc)

        for json_dir in json_dirs_to_try:
            for ep_fmt in [f"{episode_number:03d}.json", f"{episode_number:02d}.json", f"{episode_number}.json"]:
                json_path = json_dir / ep_fmt
                if json_path.exists():
                    try:
                        with open(json_path, 'r', encoding='utf-8') as f:
                            data = _json.load(f)
                        items = data if isinstance(data, list) else [data]
                        for item in items:
                            url = item.get('aliyun_url', '')
                            if url.lower().endswith('.mp3'):
                                bgm_cache = temp_dir / f"bgm_{episode_number:03d}.mp3"
                                if not bgm_cache.exists():
                                    print(f"   🎵 下载背景音乐: {url}")
                                    _urllib_req.urlretrieve(url, bgm_cache)
                                    print(f"   ✅ 下载完成: {bgm_cache.name}")
                                else:
                                    print(f"   🎵 使用缓存背景音乐: {bgm_cache.name}")
                                bgm = bgm_cache
                                break
                    except Exception as e:
                        print(f"   ⚠️  从 JSON 提取 BGM 失败: {e}")
                    break
            if bgm:
                break

    # 追踪当前处理的视频列表
    current_videos = original_videos.copy()

    # 步骤1: 黑屏检测（处理每个视频）
    if 1 not in skip_steps:
        print(f"\n{'='*60}")
        print(f"步骤1: 黑屏检测")
        print(f"{'='*60}")
        
        script = base_dir / config['paths']['scripts']['remove_black']
        step1_videos = []
        
        for video in current_videos:
            print(f"处理: {video.name}")
            cmd = [
                sys.executable, str(script),
                str(video),
                '-o', str(temp_dir),
                '--min-voice-db', '-5'
            ]
            result = subprocess.run(cmd)
            
            # 检查输出文件
            trimmed_video = temp_dir / f"{video.stem}_trimmed.mp4"
            if trimmed_video.exists():
                step1_videos.append(trimmed_video)
                print(f"  ✅ 输出: {trimmed_video.name}")
            else:
                # 如果没有生成 trimmed 文件，使用原视频
                step1_videos.append(video)
                print(f"  ⚠️  未生成 trimmed 文件，使用原视频")
        
        current_videos = step1_videos
        print(f"\n步骤1完成，当前视频列表: {[v.name for v in current_videos]}")
    
    # 步骤2: 声道分离（处理每个视频）
    if 2 not in skip_steps:
        print(f"\n{'='*60}")
        print(f"步骤2: 声道分离")
        print(f"{'='*60}")
        
        script = base_dir / config['paths']['scripts']['separate']
        conda_env = config['msst_conda_env']
        
        # 查找 conda 环境的 Python 路径
        python_exe = find_conda_env_python(conda_env)
        if not python_exe:
            print(f"❌ 无法找到 conda 环境 {conda_env}，跳过声道分离")
        else:
            step2_videos = []

            for video in current_videos:
                print(f"处理: {video.name}")

                # 调用分离脚本，直接输出到 temp_dir，不创建子目录
                cmd = [
                    str(python_exe), str(script),
                    str(video),
                    str(temp_dir),
                    '--no-subdir'
                ]
                result = subprocess.run(cmd)

                # 输出的 .wav 文件直接在 temp_dir 下，文件名为视频的 stem
                # 例如：temp_output/高手下山：美女请留步/高手下山，美女请留步01/1_trimmed.wav
                audio_output = temp_dir / f"{video.stem}.wav"

                if audio_output.exists():
                    # 替换视频音轨
                    video_with_vocals = temp_dir / f"{video.stem}_vocals.mp4"
                    if replace_audio_in_video(video, audio_output, video_with_vocals):
                        step2_videos.append(video_with_vocals)
                        print(f"  ✅ 输出: {video_with_vocals.name}")
                    else:
                        step2_videos.append(video)
                        print(f"  ⚠️  音轨替换失败，使用原视频")
                else:
                    step2_videos.append(video)
                    print(f"  ⚠️  未生成音频文件，使用原视频")

            current_videos = step2_videos
            print(f"\n步骤2完成，当前视频列表: {[v.name for v in current_videos]}")
    
    # 步骤3: 转场拼接
    if 3 not in skip_steps:
        print(f"\n{'='*60}")
        print(f"步骤3: 转场拼接")
        print(f"{'='*60}")
        
        script = base_dir / config['paths']['scripts']['transition']
        
        # 创建临时文件夹，包含所有处理后的视频
        transition_input_dir = temp_dir / "transition_input"
        transition_input_dir.mkdir(exist_ok=True)
        
        # 复制所有处理后的视频到临时文件夹（保持原始文件名顺序）
        for _, (orig_video, processed_video) in enumerate(zip(original_videos, current_videos)):
            # 使用原始文件名，确保数字顺序正确
            target = transition_input_dir / orig_video.name
            if processed_video != target:
                import shutil
                shutil.copy2(processed_video, target)
                print(f"  复制: {processed_video.name} -> {target.name}")
        
        # 如果有背景音乐，也复制到临时文件夹
        if bgm:
            bgm_target = transition_input_dir / bgm.name
            if bgm != bgm_target:
                import shutil
                shutil.copy2(bgm, bgm_target)
        
        cmd = [
            sys.executable, str(script),
            '--folder', str(transition_input_dir),
            '-o', str(output_004)  # 输出到 transition
        ]
        
        if bgm:
            cmd.extend(['--bgm', str(bgm_target)])
        
        result = subprocess.run(cmd)

        if result.returncode == 0:
            print(f"\n{'='*60}")
            print(f"✅ 视频拼接完成！")
            print(f"📁 转场输出: {output_004}")
            print(f"{'='*60}")

            # 给 transition 也添加封面（与最终视频保持一致）
            cover_image_004 = None
            for ext in ['.jpg', '.jpeg', '.png', '.JPG', '.JPEG', '.PNG']:
                cover_path = folder / f"封面{ext}"
                if cover_path.exists():
                    cover_image_004 = cover_path
                    break

            if cover_image_004 and output_004.exists():
                print(f"\n📸 给transition添加封面: {cover_image_004.name}")
                temp_004_cover = output_004.parent / f"temp_cover_{output_004.name}"
                cmd_cover = [
                    'ffmpeg', '-y',
                    '-loop', '1', '-t', '0.033',
                    '-i', str(cover_image_004),
                    '-i', str(output_004),
                    '-filter_complex',
                    '[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps=30[cover];'
                    '[1:v]fps=30[video];'
                    '[cover][video]concat=n=2:v=1:a=0[outv];'
                    '[1:a]adelay=33|33[outa]',
                    '-map', '[outv]', '-map', '[outa]',
                    '-c:v', 'libx264', '-preset', 'medium', '-crf', '23',
                    '-c:a', 'aac', '-b:a', '192k',
                    str(temp_004_cover)
                ]
                r = subprocess.run(cmd_cover, capture_output=True)
                if r.returncode == 0 and temp_004_cover.exists():
                    import shutil
                    shutil.move(str(temp_004_cover), str(output_004))
                    print(f"   ✅ transition封面添加成功")
                else:
                    print(f"   ⚠️  transition封面添加失败，保留原视频")
                    if temp_004_cover.exists():
                        temp_004_cover.unlink()
        else:
            print(f"\n❌ 拼接失败")
            sys.exit(1)
    else:
        print(f"\n⏭️  跳过步骤3: 转场拼接")
    
    # 步骤4: 字幕生成与烧录（处理拼接后的完整视频）
    if 4 not in skip_steps:
        print(f"\n{'='*60}")
        print(f"步骤4: 字幕生成与烧录")
        print(f"{'='*60}")

        script = base_dir / config['paths']['scripts']['subtitles']
        qwen_dir = base_dir / "tools" / "qwen3-asr-deployment"

        if script_dir:
            print(f"找到剧本目录: {script_dir}")
        if episode_number is not None:
            print(f"从文件夹名提取集数: {episode_number}")

        # 处理拼接后的完整视频
        print(f"处理拼接后的视频: {output_004.name}")

        # 传递配置文件的绝对路径
        config_path = base_dir / args.config
        
        # 计算字幕输出目录：保持与assets相同的目录结构
        try:
            assets_path = base_dir / config['paths']['assets']
            relative_path = folder.relative_to(assets_path)
            subtitle_output_dir = base_dir / "scripts" / "output" / "subtitle" / relative_path
        except ValueError:
            subtitle_output_dir = base_dir / "scripts" / "output" / "subtitle" / folder.name
        
        subtitle_output_dir.mkdir(parents=True, exist_ok=True)
        
        cmd = [
            sys.executable, str(script),
            '--video', str(output_004.absolute()),
            '--config', str(config_path.absolute()),
            '--output-dir', str(subtitle_output_dir.absolute()),
        ]

        # 如果找到了剧本目录，传递给字幕生成脚本
        if script_dir:
            cmd.extend(['--script-dir', str(script_dir.absolute())])

        # 如果提取到了集数，传递给字幕生成脚本
        if episode_number is not None:
            cmd.extend(['--episode', str(episode_number)])

        result = subprocess.run(cmd, cwd=str(qwen_dir))

        if result.returncode == 0:
            # 字幕烧录后的视频应该已经输出到 video 子目录
            # 检查是否生成成功
            video_output_dir = subtitle_output_dir / "video"
            expected_output = video_output_dir / output_004.name
            
            if expected_output.exists():
                # 查找封面图片
                cover_image = None
                for ext in ['.jpg', '.jpeg', '.png', '.JPG', '.JPEG', '.PNG']:
                    cover_path = folder / f"封面{ext}"
                    if cover_path.exists():
                        cover_image = cover_path
                        break
                
                # 如果有封面图片，将其作为第一帧添加到视频开头
                if cover_image:
                    print(f"\n📸 找到封面图片: {cover_image.name}")
                    print(f"   添加封面到视频开头...")
                    
                    # 创建临时文件
                    temp_with_cover = final_output.parent / f"temp_with_cover_{final_output.name}"
                    
                    # 使用ffmpeg将封面图片添加到视频开头（只显示1帧，约0.033秒）
                    cmd = [
                        'ffmpeg', '-y',
                        '-loop', '1',
                        '-t', '0.033',  # 封面只显示1帧（30fps下约0.033秒）
                        '-i', str(cover_image),
                        '-i', str(expected_output),
                        '-filter_complex',
                        '[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps=30[cover];'
                        '[1:v]fps=30[video];'
                        '[cover][video]concat=n=2:v=1:a=0[outv];'
                        '[1:a]adelay=33|33[outa]',  # 音频延迟33毫秒（1帧）
                        '-map', '[outv]',
                        '-map', '[outa]',
                        '-c:v', 'libx264',
                        '-preset', 'medium',
                        '-crf', '23',
                        '-c:a', 'aac',
                        '-b:a', '192k',
                        str(temp_with_cover)
                    ]
                    
                    result = subprocess.run(cmd, capture_output=True)
                    
                    if result.returncode == 0 and temp_with_cover.exists():
                        # 复制到最终输出位置
                        import shutil
                        shutil.copy2(temp_with_cover, final_output)
                        temp_with_cover.unlink()  # 删除临时文件
                        print(f"   ✅ 封面添加成功")
                    else:
                        print(f"   ⚠️  封面添加失败，使用原视频")
                        print(result.stderr.decode('utf-8', errors='ignore'))
                        import shutil
                        shutil.copy2(expected_output, final_output)
                else:
                    # 没有封面图片，直接复制
                    import shutil
                    shutil.copy2(expected_output, final_output)
                
                print(f"\n{'='*60}")
                print(f"✅ 全部完成！")
                print(f"📁 最终输出: {final_output}")
                print(f"{'='*60}")
            else:
                print(f"\n⚠️  字幕烧录可能失败，未找到输出文件: {expected_output}")
        else:
            print(f"\n❌ 字幕生成失败")
            sys.exit(1)
    else:
        print(f"\n⏭️  跳过步骤4: 字幕生成与烧录")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 发生错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
