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


def load_config(config_path="config.json"):
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


def parse_srt_file(srt_path):
    """解析SRT文件，返回 [{start, end, text}, ...] 列表（秒为单位）"""
    subtitles = []
    with open(srt_path, 'r', encoding='utf-8') as f:
        content = f.read()
    pattern = r'\d+\n(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})\n(.*?)(?=\n\n|\Z)'
    for start_str, end_str, text in re.findall(pattern, content, re.DOTALL):
        subtitles.append({
            'start': parse_srt_time(start_str),
            'end':   parse_srt_time(end_str),
            'text':  text.strip(),
        })
    return subtitles


def create_jianying_draft(output_004, srt_path, bgm_path, draft_name, config):
    """
    创建剪映草稿，包含三条可编辑轨道：
      视频  — output_004（已含混合BGM的音频，直接作为视频素材）
      BGM   — 原始音乐文件（独立音频轨，可替换/调音量）
      字幕  — SRT 逐句解析为文本片段
    """
    base_dir = Path(__file__).parent
    jy_lib = config.get('paths', {}).get(
        'jianying_lib',
        str(base_dir / 'libs' / 'jianying-editor-skill')
    )
    if not Path(jy_lib).exists():
        print(f"❌ 未找到剪映库: {jy_lib}")
        print(f"   请在 config.json 中配置 paths.jianying_lib，"
              f"或确认 libs/jianying-editor-skill 目录存在")
        return False

    jy_scripts = str(Path(jy_lib) / 'scripts')
    for _p in [jy_scripts, jy_lib]:
        if _p not in sys.path:
            sys.path.insert(0, _p)

    try:
        from jy_wrapper import JyProject
    except ImportError as e:
        print(f"❌ 无法导入剪映库: {e}")
        return False

    # 判断是否使用百度网盘同步输出
    sync_cfg     = config.get('draft_package', {})
    win_sync     = sync_cfg.get('windows_sync_path', '')
    mac_sync     = sync_cfg.get('mac_sync_path', '')
    draft_sub    = sync_cfg.get('draft_subfolder', '剪映草稿')
    use_sync     = bool(win_sync and mac_sync)
    drafts_root  = str(Path(win_sync) / draft_sub) if use_sync else None

    try:
        project = JyProject(draft_name, overwrite=True,
                            **({"drafts_root": drafts_root} if drafts_root else {}))

        # 1. 视频轨道
        video_duration_sec = 0.0
        if output_004 and output_004.exists():
            video_duration_sec = get_video_duration(output_004)
            project.add_media_safe(str(output_004), start_time="0s", track_name="视频")
            print(f"  ✅ 视频轨道: {output_004.name} ({video_duration_sec:.1f}s)")

        # 2. BGM 音乐轨道（截断到视频时长）
        if bgm_path and bgm_path.exists():
            bgm_duration = f"{video_duration_sec:.3f}s" if video_duration_sec > 0 else None
            project.add_audio_safe(str(bgm_path), start_time="0s",
                                   duration=bgm_duration, track_name="BGM")
            print(f"  ✅ 音乐轨道: {bgm_path.name} (截断至 {video_duration_sec:.1f}s)")
        else:
            print(f"  ⚠️  未找到BGM文件，跳过音乐轨道")

        # 3. 字幕轨道
        if srt_path and srt_path.exists():
            subtitles = parse_srt_file(srt_path)
            for sub in subtitles:
                dur = sub['end'] - sub['start']
                if dur <= 0:
                    continue
                project.add_text_simple(
                    sub['text'],
                    start_time=f"{sub['start']:.3f}s",
                    duration=f"{dur:.3f}s",
                    track_name="字幕",
                    font_size=7,
                    transform_y=-0.8,
                )
            print(f"  ✅ 字幕轨道: {len(subtitles)} 条")
        else:
            print(f"  ⚠️  未找到SRT文件，跳过字幕轨道")
            if srt_path:
                print(f"     期望路径: {srt_path}")

        project.save()

        # 同步模式：把媒体文件复制到独立目录（避免剪映删除），并把 JSON 路径改成 Mac 格式
        if use_sync:
            import json as _json, shutil as _shutil
            draft_dir = Path(drafts_root) / draft_name
            mac_draft_dir = f"{mac_sync}/{draft_sub}/{draft_name}"

            # 媒体文件放在草稿目录内
            mac_media_dir = f"{mac_sync}/{draft_sub}/{draft_name}"

            for media in [output_004, bgm_path]:
                if media and Path(media).exists():
                    _shutil.copy2(media, draft_dir / Path(media).name)
                    print(f"  📁 媒体文件 → 草稿目录: {Path(media).name}")

            draft_json_path = draft_dir / 'draft_content.json'
            with open(draft_json_path, 'r', encoding='utf-8') as f:
                d = _json.load(f)
            for sec in ['videos', 'audios']:
                for item in d.get('materials', {}).get(sec, []):
                    if item.get('path'):
                        item['path'] = f"{mac_media_dir}/{Path(item['path']).name}"
            with open(draft_json_path, 'w', encoding='utf-8') as f:
                _json.dump(d, f, ensure_ascii=False)

            # 更新 root_meta_info.json（剪映靠这个索引文件显示草稿列表）
            import time as _time, uuid as _uuid
            mac_draft_root = f"{mac_sync}/{draft_sub}"
            root_meta_path = Path(drafts_root) / 'root_meta_info.json'
            if root_meta_path.exists():
                with open(root_meta_path, 'r', encoding='utf-8') as f:
                    root_meta = _json.load(f)
            else:
                root_meta = {"all_draft_store": [], "draft_ids": 0,
                             "root_path": mac_draft_root}

            root_meta["root_path"] = mac_draft_root

            # 读取草稿ID
            meta_path = draft_dir / 'draft_meta_info.json'
            draft_id = str(_uuid.uuid4()).upper()
            if meta_path.exists():
                with open(meta_path, 'r', encoding='utf-8') as f:
                    m = _json.load(f)
                draft_id = m.get('draft_id', draft_id)

            now_us = int(_time.time() * 1_000_000)
            dur_us = int(video_duration_sec * 1_000_000)

            new_entry = {
                "draft_cloud_last_action_download": False,
                "draft_cloud_purchase_info": "",
                "draft_cloud_template_id": "",
                "draft_cloud_tutorial_info": "",
                "draft_cloud_videocut_purchase_info": "",
                "draft_cover": f"{mac_draft_dir}/draft_cover.jpg",
                "draft_fold_path": mac_draft_dir,
                "draft_id": draft_id,
                "draft_is_ai_shorts": False,
                "draft_is_invisible": False,
                "draft_json_file": f"{mac_draft_dir}/draft_content.json",
                "draft_name": draft_name,
                "draft_new_version": "",
                "draft_root_path": mac_draft_root,
                "draft_timeline_materials_size": 0,
                "draft_type": "",
                "tm_draft_cloud_completed": "",
                "tm_draft_cloud_modified": 0,
                "tm_draft_create": now_us,
                "tm_draft_modified": now_us,
                "tm_draft_removed": 0,
                "tm_duration": dur_us,
            }

            # 替换同名草稿或追加
            store = root_meta.get("all_draft_store", [])
            store = [e for e in store if e.get("draft_name") != draft_name]
            store.insert(0, new_entry)
            root_meta["all_draft_store"] = store

            with open(root_meta_path, 'w', encoding='utf-8') as f:
                _json.dump(root_meta, f, ensure_ascii=False)

            print(f"✅ 草稿已同步到百度网盘: {draft_dir}")
            print(f"   Mac 剪映草稿目录请设为: {mac_draft_root}/")
        else:
            print(f"✅ 剪映草稿已生成: {draft_name}（重启剪映后可见）")
        return True

    except Exception as e:
        print(f"❌ 创建剪映草稿失败: {e}")
        import traceback
        traceback.print_exc()
        return False


# 安装草稿.command 模板（Mac双击即运行，python3内嵌）
_INSTALL_COMMAND_TEMPLATE = r'''#!/bin/bash
cd "$(dirname "$0")"
python3 - << 'PYEOF'
import os, json, shutil, sys
from pathlib import Path

DRAFT_NAME = "PLACEHOLDER_DRAFT_NAME"

here = Path(os.environ.get("BASH_SOURCE_DIR", ".")).resolve()
# 从当前工作目录找文件（bash已cd到脚本所在目录）
here = Path.cwd()

jy_root = Path.home() / "Movies" / "JianyingPro" / "User Data" / "Projects" / "com.lveditor.draft"
if not jy_root.exists():
    print("未找到剪映草稿目录，请确认已安装剪映Pro")
    sys.exit(1)

draft_dir = jy_root / DRAFT_NAME
draft_dir.mkdir(parents=True, exist_ok=True)

for ext in ["*.mp4", "*.mp3", "*.flac", "*.wav", "*.m4a"]:
    for f in here.glob(ext):
        shutil.copy2(f, draft_dir / f.name)
        print("  复制: " + f.name)

with open(here / "draft_content.json", "r", encoding="utf-8") as fp:
    draft = json.load(fp)
for section in ["videos", "audios"]:
    for item in draft.get("materials", {}).get(section, []):
        if item.get("path"):
            item["path"] = str(draft_dir / Path(item["path"]).name)
with open(draft_dir / "draft_content.json", "w", encoding="utf-8") as fp:
    json.dump(draft, fp, ensure_ascii=False)

meta = here / "draft_meta_info.json"
if meta.exists():
    shutil.copy2(meta, draft_dir / "draft_meta_info.json")

print("✅ 草稿已安装: " + DRAFT_NAME)
print("重启剪映后即可查看")
PYEOF
echo "按任意键关闭..."
read -n 1
'''


def package_jianying_draft(draft_name, pkg_output_dir, config):
    """
    将剪映草稿打包为可分发的zip。
    源草稿来自 BaiduSyncdisk（Step5生成、Mac编辑后同步回来的版本）。
    zip 内含媒体文件 + install.py，对方 python3 install.py 即可自动安装。
    """
    import json as _json, shutil, zipfile

    sync_cfg  = config.get('draft_package', {})
    win_sync  = sync_cfg.get('windows_sync_path', '')
    draft_sub = sync_cfg.get('draft_subfolder', 'JianyingPro Drafts')

    # 源草稿目录：Step5 生成并由 Mac 编辑后同步回来的位置
    draft_dir  = Path(win_sync) / draft_sub / draft_name
    draft_json = draft_dir / 'draft_content.json'
    if not draft_json.exists():
        print(f"❌ 未找到草稿（请先运行步骤5并等待Mac编辑完同步回来）: {draft_json}")
        return False

    with open(draft_json, 'r', encoding='utf-8') as f:
        draft = _json.load(f)

    # 将 JSON 路径替换为占位符，install.py 在对方机器上替换成实际路径
    _PLACEHOLDER = "__DRAFT_DIR__"
    for section in ['videos', 'audios']:
        for item in draft.get('materials', {}).get(section, []):
            if item.get('path'):
                fname = Path(item['path']).name
                item['path'] = f"{_PLACEHOLDER}/{fname}"

    # 临时目录
    base_dir = Path(__file__).parent
    tmp_dir = base_dir / 'temp_pkg' / draft_name
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)

    # 写入占位符版 JSON
    with open(tmp_dir / 'draft_content.json', 'w', encoding='utf-8') as f:
        _json.dump(draft, f, ensure_ascii=False)
    meta = draft_dir / 'draft_meta_info.json'
    if meta.exists():
        shutil.copy2(meta, tmp_dir / 'draft_meta_info.json')

    # 复制媒体文件（草稿目录内的 mp4/mp3 等）
    media_count = 0
    for f in draft_dir.iterdir():
        if f.suffix.lower() in {'.mp4', '.mp3', '.flac', '.wav', '.m4a'}:
            shutil.copy2(f, tmp_dir / f.name)
            print(f"  打包素材: {f.name}")
            media_count += 1
    if media_count == 0:
        print(f"  ⚠️  草稿目录内未找到媒体文件，请确认步骤5已运行")

    # 生成 install.py（对方运行后自动安装到剪映）
    install_py = f'''\
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""双击或 python3 install.py 即可将草稿安装到本机剪映"""
import os, json, shutil, re
from pathlib import Path

DRAFT_NAME = {repr(draft_name)}

# 常见剪映 Mac 草稿目录（按优先级尝试）
CANDIDATES = [
    Path.home() / "Movies" / "JianyingPro" / "User Data" / "Projects" / "com.lveditor.draft",
    Path.home() / "Library" / "Containers" / "com.lveditor.draft.macEdition" /
        "Data" / "Movies" / "JianyingPro" / "User Data" / "Projects" / "com.lveditor.draft",
]

jy_root = next((p for p in CANDIDATES if p.exists()), None)
if jy_root is None:
    # 尝试在 Movies 目录下递归查找
    for p in (Path.home() / "Movies").rglob("com.lveditor.draft"):
        if p.is_dir():
            jy_root = p
            break

if jy_root is None:
    print("❌ 未找到剪映草稿目录，请确认已安装剪映并至少打开过一次")
    input("按回车键退出...")
    raise SystemExit(1)

dst = jy_root / DRAFT_NAME
dst.mkdir(parents=True, exist_ok=True)

here = Path(__file__).parent / DRAFT_NAME
for f in here.iterdir():
    if f.name == "install.py":
        continue
    shutil.copy2(f, dst / f.name)

# 修复 draft_content.json 路径
dj = dst / "draft_content.json"
with open(dj, encoding="utf-8") as fh:
    text = fh.read()
text = text.replace("__DRAFT_DIR__", str(dst).replace("\\\\", "/"))
with open(dj, "w", encoding="utf-8") as fh:
    fh.write(text)

# 更新 root_meta_info.json
import time, uuid
root_meta_path = jy_root / "root_meta_info.json"
if root_meta_path.exists():
    with open(root_meta_path, encoding="utf-8") as fh:
        rm = json.load(fh)
else:
    rm = {{"all_draft_store": [], "draft_ids": 0, "root_path": str(jy_root)}}

meta_file = dst / "draft_meta_info.json"
draft_id = str(uuid.uuid4()).upper()
if meta_file.exists():
    with open(meta_file, encoding="utf-8") as fh:
        draft_id = json.load(fh).get("draft_id", draft_id)

entry = {{
    "draft_cloud_last_action_download": False,
    "draft_cloud_purchase_info": "", "draft_cloud_template_id": "",
    "draft_cloud_tutorial_info": "", "draft_cloud_videocut_purchase_info": "",
    "draft_cover": str(dst / "draft_cover.jpg"),
    "draft_fold_path": str(dst),
    "draft_id": draft_id,
    "draft_is_ai_shorts": False, "draft_is_invisible": False,
    "draft_json_file": str(dst / "draft_content.json"),
    "draft_name": DRAFT_NAME,
    "draft_new_version": "", "draft_root_path": str(jy_root),
    "draft_timeline_materials_size": 0, "draft_type": "",
    "tm_draft_cloud_completed": "", "tm_draft_cloud_modified": 0,
    "tm_draft_create": int(time.time() * 1_000_000),
    "tm_draft_modified": int(time.time() * 1_000_000),
    "tm_draft_removed": 0, "tm_duration": 0,
}}
store = [e for e in rm.get("all_draft_store", []) if e.get("draft_name") != DRAFT_NAME]
store.insert(0, entry)
rm["all_draft_store"] = store
with open(root_meta_path, "w", encoding="utf-8") as fh:
    json.dump(rm, fh, ensure_ascii=False)

print("✅ 草稿已安装:", DRAFT_NAME)
print("   路径:", dst)
print("重启剪映后即可查看")
input("按回车键退出...")
'''
    (tmp_dir / 'install.py').write_text(install_py, encoding='utf-8')

    # 打包：zip 根目录 = draft_name/，install.py 在外层
    pkg_output_dir.mkdir(parents=True, exist_ok=True)
    zip_path = pkg_output_dir / f"{draft_name}_jydraft.zip"
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in tmp_dir.iterdir():
            if f.name == 'install.py':
                zf.write(f, 'install.py')
            else:
                zf.write(f, f"{draft_name}/{f.name}")

    shutil.rmtree(tmp_dir)
    print(f"✅ 打包完成: {zip_path}")
    print(f"   发给对方后，解压，运行 python3 install.py 即可")
    return zip_path


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


def merge_and_adjust_subtitles(srt_files, output_srt, video_folder):
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
    config_path = Path(__file__).parent / "config.json"
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


def find_audio(folder):
    """查找背景音乐文件"""
    for ext in ['.flac', '.mp3', '.wav', '.m4a']:
        audio_files = list(folder.glob(f"*{ext}"))
        if audio_files:
            return audio_files[0]
    return None


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
  python run_all.py --folder assets/太古凌霄，唯我独尊/太古凌霄68
  python run_all.py --folder assets/项目名/集数 --skip 2
        """
    )
    
    parser.add_argument('--folder', '-f', required=True, help='视频文件夹路径')
    parser.add_argument('--config', '-c', default='config.json', help='配置文件路径')
    parser.add_argument('--skip', nargs='+', type=int, choices=[1, 2, 3, 4, 5],
                       help='跳过的步骤（1=黑屏 2=分离 3=转场 4=字幕 5=剪映草稿）')
    parser.add_argument('--no-bgm', action='store_true',
                       help='不添加背景音乐（生成纯人声视频，用于 select_music 测试）')

    args = parser.parse_args()
    
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
            _skip_steps_pre = set(args.skip or [])
            # 各要素对应的步骤：音乐/封面 → 步骤3，剧本 → 步骤4
            _need_bgm    = 3 not in _skip_steps_pre
            _need_cover  = 3 not in _skip_steps_pre
            _need_script = 4 not in _skip_steps_pre
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
                _required_ok = (
                    (not _need_bgm    or _bgm_ok) and
                    (not _need_cover  or _cover_ok) and
                    (not _need_script or _ep_ok)
                )
                if not _required_ok:
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
                if args.config != 'config.json':
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
    if getattr(args, 'no_bgm', False):
        bgm = None

    print(f"\n{'='*60}")
    print(f"开始处理: {folder.name}")
    print(f"{'='*60}")
    print(f"找到 {len(original_videos)} 个视频文件")
    if bgm:
        print(f"找到背景音乐: {bgm.name}")
    elif getattr(args, 'no_bgm', False):
        print(f"⏭️  --no-bgm：跳过背景音乐")
    
    skip_steps = args.skip or []
    
    # 创建输出目录结构：保持与assets相同的目录层级
    try:
        assets_path = base_dir / config['paths']['assets']
        relative_path = folder.relative_to(assets_path)

        # 004output: 转场后的视频（未烧录字幕），保持与assets相同的目录结构
        # 例如：assets/高手下山：美女请留步/高手下山，美女请留步01
        #   -> output/004output/高手下山：美女请留步/高手下山，美女请留步01.mp4
        output_004_dir = base_dir / config['paths']['output'] / "004output" / relative_path.parent
        output_004_dir.mkdir(parents=True, exist_ok=True)
        output_004 = output_004_dir / f"{folder.name}.mp4"

        # 003output: 字幕烧录后的最终视频，保持与assets相同的目录结构
        output_003_dir = base_dir / config['paths']['output'] / "003output" / relative_path.parent
        output_003_dir.mkdir(parents=True, exist_ok=True)
        final_output = output_003_dir / f"{folder.name}.mp4"
    except ValueError:
        # 如果不在assets下，使用简化的输出结构
        output_004_dir = base_dir / config['paths']['output'] / "004output"
        output_004_dir.mkdir(parents=True, exist_ok=True)
        output_004 = output_004_dir / f"{folder.name}.mp4"

        output_003_dir = base_dir / config['paths']['output'] / "003output"
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
        for i, (orig_video, processed_video) in enumerate(zip(original_videos, current_videos)):
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
            '-o', str(output_004)  # 输出到 004output
        ]

        # 传入剧本路径，避免退化到阈值模式
        if script_dir and episode_number is not None:
            for fmt in [f"Episode-{episode_number:02d}.md", f"Episode-{episode_number:03d}.md"]:
                ep_script = script_dir / fmt
                if ep_script.exists():
                    cmd.extend(['--script', str(ep_script)])
                    break

        if bgm:
            cmd.extend(['--bgm', str(bgm_target)])
        
        result = subprocess.run(cmd)

        if result.returncode == 0:
            print(f"\n{'='*60}")
            print(f"✅ 视频拼接完成！")
            print(f"📁 转场输出: {output_004}")
            print(f"{'='*60}")

            # 给 004output 也添加封面（与最终视频保持一致）
            cover_image_004 = None
            for ext in ['.jpg', '.jpeg', '.png', '.JPG', '.JPEG', '.PNG']:
                cover_path = folder / f"封面{ext}"
                if cover_path.exists():
                    cover_image_004 = cover_path
                    break

            if cover_image_004 and output_004.exists():
                print(f"\n📸 给004output添加封面: {cover_image_004.name}")
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
                    print(f"   ✅ 004output封面添加成功")
                else:
                    print(f"   ⚠️  004output封面添加失败，保留原视频")
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
        qwen_dir = base_dir / "qwen3-asr-deployment"

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

    # 步骤5: 生成剪映草稿（调用 select_music.py：Kimi 选曲 + 多段 BGM + 剪映草稿）
    if 5 not in skip_steps:
        print(f"\n{'='*60}")
        print(f"步骤5: 生成剪映草稿")
        print(f"{'='*60}")

        select_music_script = base_dir / 'scripts' / 'select_music.py'
        try:
            folder_rel = str(folder.relative_to(base_dir))
        except ValueError:
            folder_rel = str(folder)

        cmd = [sys.executable, str(select_music_script), '--folder', folder_rel]
        result = subprocess.run(cmd, cwd=str(base_dir))
        if result.returncode != 0:
            print(f"\n❌ 剪映草稿生成失败")
            sys.exit(1)
    else:
        print(f"\n⏭️  跳过步骤5: 生成剪映草稿")



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
