# -*- coding: utf-8 -*-
"""
配乐选曲 + 生成剪映草稿测试脚本

流程：
  1. 从 --folder 推断集数，找到对应剧本
  2. 加载 assets/音乐/ 下所有 music_tags.json
  3. 调 Kimi 导演 skill 选出 BGM
  4. 在 output/003output/{folder_name}/ 找到视频文件
  5. 用选好的 BGM + 视频创建剪映草稿并同步到百度网盘

用法:
  python scripts/select_music.py --folder assets/太古凌霄：唯我独尊10
  python scripts/select_music.py --folder "assets/下山退婚：逍遥神医/神医-10"
"""

import argparse
import io
import json
import re
import sys
from pathlib import Path

import requests

# 确保 UTF-8 输出（Windows GBK 终端兼容）
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ── 常量 ───────────────────────────────────────────────────────────────────
KIMI_BASE_URL = "https://api.moonshot.cn/v1"
MODEL = "kimi-k2.5"
MUSIC_ROOT = Path("assets/音乐")
DIRECTOR_SKILL_PATH = Path(__file__).parent.parent / "skills" / "music_director.md"


# ── 工具函数 ────────────────────────────────────────────────────────────────

def load_config(config_path="config.json") -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_skill_prompt(skill_path: Path) -> str:
    text = skill_path.read_text(encoding="utf-8")
    return re.sub(r"^---.*?---\s*", "", text, flags=re.DOTALL).strip()


def load_all_music_tags(music_root: Path) -> dict:
    """遍历 assets/音乐/completed/ 下所有子文件夹，收集 music_tags.json。
    completed/ 不存在时回退到 music_root 根目录（兼容旧结构）。"""
    catalog = {}
    completed_dir = music_root / "completed"
    search_root = completed_dir if completed_dir.is_dir() else music_root
    for tags_file in search_root.rglob("music_tags.json"):
        folder_name = tags_file.parent.name
        try:
            tags = json.loads(tags_file.read_text(encoding="utf-8"))
            for filename, tag in tags.items():
                key = f"{folder_name}/{filename}"
                catalog[key] = {**tag, "_folder": folder_name, "_file": filename}
        except Exception as e:
            print(f"  [警告] 读取 {tags_file} 失败: {e}")
    return catalog


def format_catalog_for_prompt(catalog: dict) -> str:
    lines = ["曲库列表（格式：文件名 | 子文件夹 | 情绪 | 能量 | 风格 | 推荐位置 | 人声）：", ""]
    for tag in catalog.values():
        emotion = "/".join(tag.get("emotion", []))
        energy = tag.get("energy", "")
        style = "/".join(tag.get("style", []))
        placement = "/".join(tag.get("placement", []))
        vocal = tag.get("vocal", "")
        folder = tag.get("_folder", "")
        fname = tag.get("_file", "")
        lines.append(f"{fname} | {folder} | {emotion} | {energy} | {style} | {placement} | {vocal}")
    return "\n".join(lines)


def find_episode_script(folder: Path):
    """从文件夹推断集数，找到对应的剧本 .md 文件。返回 (episode_num, script_path)"""
    folder_name = folder.name
    m = re.search(r'[-_]?(\d+)$', folder_name)
    if not m:
        print(f"  [错误] 无法从文件夹名 '{folder_name}' 提取集数")
        return None, None
    episode_num = int(m.group(1))

    # 先在当前文件夹内找 *设定集
    script_dirs = [d for d in folder.iterdir() if d.is_dir() and "设定集" in d.name]
    if not script_dirs:
        # 再到上级目录找
        script_dirs = [d for d in folder.parent.iterdir() if d.is_dir() and "设定集" in d.name]

    if not script_dirs:
        print(f"  [错误] 找不到设定集文件夹（在 {folder} 或其父目录）")
        return None, None

    script_dir = script_dirs[0]
    for name in [f"Episode-{episode_num:02d}.md", f"Episode-{episode_num}.md"]:
        p = script_dir / name
        if p.exists():
            return episode_num, p

    print(f"  [错误] 找不到 Episode-{episode_num:02d}.md，设定集目录: {script_dir}")
    return None, None


def find_output_video(folder: Path) -> Path:
    """
    优先用 004output（干净视频，无烧录字幕），回退到 003output。
    支持有/无父目录两种结构。
    """
    for output_dir in ["004output", "003output"]:
        base = Path.cwd() / "output" / output_dir
        out_dir = base / folder.name
        if out_dir.exists():
            mp4s = sorted(out_dir.glob("*.mp4"))
            if mp4s:
                if len(mp4s) > 1:
                    print(f"  [提示] 找到多个 mp4，使用第一个: {mp4s[0].name}")
                print(f"  使用 {output_dir}: {mp4s[0].name}")
                return mp4s[0]
        candidates = sorted(base.rglob(f"{folder.name}.mp4"))
        if candidates:
            if len(candidates) > 1:
                print(f"  [提示] 找到多个匹配，使用: {candidates[0]}")
            print(f"  使用 {output_dir}: {candidates[0].name}")
            return candidates[0]
    print(f"  [错误] 在 004output/003output 下均找不到 {folder.name}.mp4")
    return None


def find_srt(folder: Path) -> Path:
    """在 scripts/output/subtitle/ 下找对应的 optimized SRT 文件。"""
    base = Path.cwd() / "scripts" / "output" / "subtitle"
    candidates = sorted(base.rglob(f"{folder.name}_qwen3_optimized.srt"))
    if candidates:
        return candidates[0]
    return None


def find_clips_manifest(video_path: Path) -> list:
    """在同目录下查找 {video_stem}_clips.json 片段清单（由 004transition_v2.py 生成）。"""
    manifest_path = video_path.parent / f"{video_path.stem}_clips.json"
    if manifest_path.exists():
        try:
            return json.loads(manifest_path.read_text(encoding='utf-8'))
        except Exception as e:
            print(f"  [警告] 读取 clips manifest 失败: {e}")
    return None


def resolve_bgm_path(selection: dict) -> Path:
    """从选曲结果解析出本地音频文件路径，优先从 completed/ 查找。"""
    folder = selection.get("folder", "")
    filename = selection.get("file", "")
    base = Path.cwd() / MUSIC_ROOT
    # 优先 completed/
    for candidate in [
        base / "completed" / folder / filename,
        base / folder / filename,
    ]:
        if candidate.exists():
            return candidate
    return None


# ── Kimi API ────────────────────────────────────────────────────────────────

def build_scene_change_context(clips_manifest: list, video_dur_s: float) -> str:
    """从 clips manifest 提取场景切换时刻及色彩特征，格式化为给 Kimi 的上下文文本。"""
    if not clips_manifest or video_dur_s <= 0:
        return ""

    def color_desc(clip: dict) -> str:
        p = clip.get("color_profile", {})
        if not p:
            return ""
        # 只输出色温和亮度；短剧饱和度普遍偏低，不作情绪信号
        parts = [p.get("warmth", ""), p.get("brightness", "")]
        return "，".join(x for x in parts if x)

    changes = []
    for i, clip in enumerate(clips_manifest):
        if clip.get("has_fade_after") and i + 1 < len(clips_manifest):
            t = clip["start_s"] + clip["duration_s"]
            pct = round(t / video_dur_s * 100)
            next_clip = clips_manifest[i + 1]
            line = f"  - {t:.1f}s（{pct}%）：{clip['file']} → {next_clip['file']}"
            cd_before = color_desc(clip)
            cd_after  = color_desc(next_clip)
            if cd_before:
                line += f"\n      转场前画面：{cd_before}"
            if cd_after:
                line += f"\n      转场后画面：{cd_after}"
            changes.append(line)

    if not changes:
        return f"\n【视频数据】总时长：{video_dur_s:.1f}s，未检测到大场景切换。"
    lines = [
        f"\n【视频数据】总时长：{video_dur_s:.1f}s",
        "检测到的大场景切换点（BGM 切换必须优先对齐这些时刻）：",
        "（色温/亮度仅供参考光线氛围，短剧整体饱和度偏低属正常，请勿因此判定情绪低落）",
    ] + changes
    return "\n".join(lines)


def _do_kimi_request(api_key: str, messages: list, model: str) -> dict | None:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": messages, "temperature": 1}
    try:
        resp = requests.post(f"{KIMI_BASE_URL}/chat/completions",
                             headers=headers, json=payload, timeout=300)
        if resp.status_code != 200:
            print(f"  [Kimi 失败 {resp.status_code}]: {resp.text[:300]}")
            return None
        content = resp.json()["choices"][0]["message"]["content"]
        match = re.search(r"\{[\s\S]*\}", content)
        return json.loads(match.group()) if match else None
    except Exception as e:
        print(f"  [请求异常]: {e}")
        return None


def call_kimi_director(api_key: str, system_prompt: str, script_text: str,
                       catalog_text: str, model: str,
                       scene_context: str = "") -> dict:
    user_content = (
        f"以下是本集剧本：\n\n---\n{script_text}\n---\n\n"
        f"以下是可用曲库：\n\n{catalog_text}\n\n"
        + (f"{scene_context}\n\n" if scene_context else "")
        + "请阅读剧本，分析情绪弧线，从曲库中为本集选出 2-4 首 BGM，"
        "严格只输出 JSON，不要任何解释文字。"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_content},
    ]
    result = _do_kimi_request(api_key, messages, model)
    if not result:
        return None

    # ── 验证选曲文件是否存在，不存在则重试一次 ──────────────────────────────
    missing = [
        sel.get("file", "") for sel in result.get("selections", [])
        if not resolve_bgm_path(sel)
    ]
    if missing:
        missing_str = "、".join(missing)
        print(f"  [验证] 以下文件在曲库中不存在，重新选曲: {missing_str}")
        messages.append({"role": "assistant", "content": json.dumps(result, ensure_ascii=False)})
        messages.append({
            "role": "user",
            "content": (
                f"以下文件名在曲库中不存在：{missing_str}。\n"
                "请重新只从上面提供的曲库列表中选择，"
                "确保每个 file 字段完全匹配曲库中的文件名（含扩展名）。"
                "严格只输出 JSON。"
            ),
        })
        retry = _do_kimi_request(api_key, messages, model)
        if retry:
            result = retry

    return result


# ── 创建剪映草稿（复用 run_all_v3 的函数）────────────────────────────────────

def get_video_duration_s(video_path: Path) -> float:
    """用 ffprobe 获取视频时长（秒）"""
    import subprocess
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True, timeout=30
        )
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def get_normalized_volume(bgm_path: Path, usage: str = "",
                          min_vol: float = 0.05, max_vol: float = 0.40) -> float:
    """
    用 ffmpeg volumedetect 测量 BGM 平均响度，
    计算使其达到统一目标响度所需的 volume 系数。

    target_db=-30 dB：所有 BGM 归一化到同一响度，
    作为短剧对白背景音乐的舒适水平。
    各歌曲原始响度不同，volume 系数会因曲而异，
    但最终播放出来的感知响度相同。
    """
    import subprocess, re

    target_db = -34.0  # 统一目标，不按场景分级

    try:
        r = subprocess.run(
            ["ffmpeg", "-i", str(bgm_path),
             "-filter:a", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60
        )
        m = re.search(r"mean_volume:\s*([-\d.]+)\s*dB", r.stderr)
        if not m:
            return 0.20  # 无法测量时回退固定值
        mean_db = float(m.group(1))
        gain_db = target_db - mean_db
        volume = 10 ** (gain_db / 20.0)
        volume = max(min_vol, min(volume, max_vol))
        return round(volume, 3)
    except Exception:
        return 0.20


def _parse_srt(srt_path: Path) -> list:
    """解析 SRT 文件，返回 [{'text', 'start', 'end'}, ...]"""
    import re as _re
    subtitles = []
    text = srt_path.read_text(encoding='utf-8')
    blocks = _re.split(r'\n\s*\n', text.strip())
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        # 时间行
        m = _re.match(r'(\d+:\d+:\d+[,\.]\d+)\s*-->\s*(\d+:\d+:\d+[,\.]\d+)', lines[1])
        if not m:
            continue
        def _ts(s):
            s = s.replace(',', '.')
            h, m_, rest = s.split(':')
            return int(h)*3600 + int(m_)*60 + float(rest)
        start, end = _ts(m.group(1)), _ts(m.group(2))
        txt = ' '.join(lines[2:]).strip()
        if txt:
            subtitles.append({'text': txt, 'start': start, 'end': end})
    return subtitles


def create_draft_multi_bgm(video_path: Path, bgm_list: list, draft_name: str,
                            config: dict, bgm_volume: float = 0.5,
                            srt_path: Path = None,
                            clips_manifest: list = None,
                            source_folder: Path = None) -> bool:
    """
    创建剪映草稿，支持多首 BGM 按时间分段铺满视频。
    bgm_list: [(Path, start_s, usage_label), ...]
    bgm_volume: BGM 音量（0~1，默认 0.5）

    策略：在本地临时目录生成草稿，再覆写 BaiduSyncdisk 里的文件，
    避免 shutil.rmtree 触发百度云盘把旧内容重新同步回来。
    """
    import json as _json, shutil as _shutil, tempfile as _tempfile
    import time as _time, uuid as _uuid

    base_dir = Path(__file__).parent.parent
    jy_lib = config.get('paths', {}).get('jianying_lib',
                                          str(base_dir / 'libs' / 'jianying-editor-skill'))
    jy_scripts = str(Path(jy_lib) / 'scripts')
    for p in [jy_scripts, jy_lib]:
        if p not in sys.path:
            sys.path.insert(0, p)

    try:
        from jy_wrapper import JyProject
        import pyJianYingDraft as _draft
    except ImportError as e:
        print(f"  [错误] 无法导入剪映库: {e}")
        return False

    sync_cfg  = config.get('draft_package', {})
    win_sync  = sync_cfg.get('windows_sync_path', '')
    mac_sync  = sync_cfg.get('mac_sync_path', '')
    draft_sub = sync_cfg.get('draft_subfolder', 'JianyingPro Drafts')
    use_sync  = bool(win_sync and mac_sync)

    try:
        CROSSFADE_S = 2.0  # 相邻 BGM 重叠淡入淡出时长（秒）

        # ── Step A：在临时目录里建草稿，避免直接操作 BaiduSyncdisk ──────────────
        tmp_root = _tempfile.mkdtemp(prefix="jy_draft_")
        try:
            project = JyProject(draft_name, overwrite=True, drafts_root=tmp_root)

            # 1. 视频轨道
            video_dur_s = get_video_duration_s(video_path)
            used_media = [video_path]
            if clips_manifest and source_folder:
                # 黑场素材路径（用于场景切换处的淡出-黑场-淡入效果）
                BLACK_SCREEN_PATH = Path(__file__).parent.parent / "assets" / "黑场.mp4"
                FADE_DUR = "0.3s"
                black_dur = 0.0
                if BLACK_SCREEN_PATH.exists():
                    black_dur = get_video_duration_s(BLACK_SCREEN_PATH)
                    print(f"  黑场素材: {BLACK_SCREEN_PATH.name} ({black_dur:.2f}s)")
                else:
                    print(f"  [警告] 黑场素材不存在: {BLACK_SCREEN_PATH}，场景切换处将跳过黑场插入")

                # 把每个源文件分别加到轨道，用整数 µs 累加游标避免浮点截断导致的 1µs 重叠
                added = 0
                cursor_us = 0  # 当前时间轴位置（整数微秒）
                black_dur_us = int(black_dur * 1_000_000)
                for clip in clips_manifest:
                    clip_path = source_folder / clip["file"]
                    if not clip_path.exists():
                        print(f"  [警告] 源文件不存在，跳过: {clip_path}")
                        continue
                    # 按 trim_frames 跳过对应帧（兼容旧格式 trim_first_frame）
                    tf = clip.get("trim_frames",
                                  1 if clip.get("trim_first_frame") else 0)
                    src_start_us = int(tf / 30 * 1_000_000)
                    clip_dur_us = int(clip["duration_s"] * 1_000_000)
                    seg = project.add_media_safe(
                        str(clip_path),
                        start_time=cursor_us,       # int → safe_tim 直接视为 µs
                        duration=clip_dur_us,        # int → safe_tim 直接视为 µs
                        source_start=src_start_us,   # int → safe_tim 直接视为 µs
                        track_name="视频",
                    )
                    cursor_us += clip_dur_us
                    # 场景切换处：结尾叠化淡出 → 插入黑场片段 → 黑场结尾叠化淡入
                    if seg and clip.get("has_fade_after") and BLACK_SCREEN_PATH.exists() and black_dur > 0:
                        try:
                            # 当前片段结尾叠化淡出（→ 黑场）
                            seg.add_transition(_draft.TransitionType.叠化, duration=FADE_DUR)
                            if seg.transition is not None and \
                                    seg.transition not in project.script.materials.transitions:
                                project.script.materials.transitions.append(seg.transition)
                            # 插入黑场片段
                            black_seg = project.add_media_safe(
                                str(BLACK_SCREEN_PATH),
                                start_time=cursor_us,
                                duration=black_dur_us,
                                track_name="视频",
                            )
                            if black_seg:
                                # 黑场结尾叠化淡入（→ 下一片段）
                                black_seg.add_transition(_draft.TransitionType.叠化, duration=FADE_DUR)
                                if black_seg.transition is not None and \
                                        black_seg.transition not in project.script.materials.transitions:
                                    project.script.materials.transitions.append(black_seg.transition)
                                if BLACK_SCREEN_PATH not in used_media:
                                    used_media.append(BLACK_SCREEN_PATH)
                                cursor_us += black_dur_us
                                print(f"  黑场: {clip['file']} → 黑场({black_dur:.2f}s) → 下一片段 [叠化 {FADE_DUR}]")
                        except Exception as _te:
                            print(f"  [警告] 黑场转场添加失败: {_te}")
                    if clip_path not in used_media:
                        used_media.append(clip_path)
                    added += 1
                print(f"  视频轨道: {added} 个独立源文件片段 ({video_dur_s:.1f}s)")
            else:
                project.add_media_safe(str(video_path), start_time="0s", track_name="视频")
                print(f"  视频轨道: {video_path.name} ({video_dur_s:.1f}s)")

            # 2. 多段 BGM：等分切割，段间 {CROSSFADE_S}s 重叠淡入淡出
            # bgm_list: [(path, split_start_s, label), ...]
            # split_start_s 是"切换点"，实际片段会往前/后延伸 CROSSFADE_S
            n = len(bgm_list)
            # 构建切换点列表（含结尾）
            split_points = [item[1] for item in bgm_list] + [video_dur_s]

            for i, (bgm_path, split_start_s, label) in enumerate(bgm_list):
                split_end_s = split_points[i + 1]
                is_last = (i == n - 1)

                # 实际在时间轴上的起止：
                # 每侧只延伸 CROSSFADE_S/2，使切换点处两轨各处于 50% 音量（真正交叉淡化）
                actual_start_s = max(0.0, split_start_s - (CROSSFADE_S / 2 if i > 0 else 0.0))
                # 最后一首必须覆盖到视频结尾
                if is_last:
                    actual_end_s = video_dur_s
                else:
                    actual_end_s = min(video_dur_s, split_end_s + CROSSFADE_S / 2)
                duration_s = actual_end_s - actual_start_s

                if duration_s <= 0:
                    continue

                # 非最后一首：限制在文件物理时长内（防止静音尾巴）
                if not is_last:
                    phys_dur_s = get_video_duration_s(bgm_path)
                    if phys_dur_s > 0:
                        duration_s = min(duration_s, phys_dur_s)

                seg = project.add_audio_safe(
                    str(bgm_path),
                    start_time=f"{actual_start_s:.3f}s",
                    duration=f"{duration_s:.3f}s",
                    track_name="BGM",
                )
                if seg:
                    # 按实际响度 + 场景类型归一化
                    seg.volume = get_normalized_volume(bgm_path, usage=label)
                    # 淡入（非首段）、淡出（所有段都加，最后一首用固定 2s）
                    fade_in_us  = int(CROSSFADE_S * 1_000_000) if i > 0 else 0
                    fade_out_us = int(CROSSFADE_S * 1_000_000) if not is_last else int(2.0 * 1_000_000)
                    if fade_in_us or fade_out_us:
                        seg.add_fade(fade_in_us, fade_out_us)
                        # add_segment 先于 add_fade 执行，fade 不会被自动注册到 materials
                        # 需要手动追加，否则 audio_fades 列表为空，剪映看不到淡化效果
                        if seg.fade is not None and seg.fade not in project.script.materials.audio_fades:
                            project.script.materials.audio_fades.append(seg.fade)
                    print(f"  BGM [{label}] {bgm_path.name}  "
                          f"{actual_start_s:.0f}s→{actual_end_s:.0f}s  "
                          f"vol={seg.volume}  淡入={fade_in_us//1_000_000}s 淡出={fade_out_us//1_000_000}s")
                    if bgm_path not in used_media:
                        used_media.append(bgm_path)

            # 3. 字幕轨道（来自 SRT，可在剪映里编辑）
            if srt_path and srt_path.exists():
                subtitles = _parse_srt(srt_path)
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
                print(f"  字幕轨道: {len(subtitles)} 条（来自 {srt_path.name}）")

            project.save()

            # 3. 读取临时目录里刚生成的 draft_content.json
            tmp_draft_dir = Path(tmp_root) / draft_name
            tmp_json_path = tmp_draft_dir / 'draft_content.json'
            with open(tmp_json_path, 'r', encoding='utf-8') as f:
                d = _json.load(f)

        finally:
            # 临时目录用完即删
            _shutil.rmtree(tmp_root, ignore_errors=True)

        # ── Step B：修正路径为 Mac 格式 ──────────────────────────────────────
        if use_sync:
            drafts_root = str(Path(win_sync) / draft_sub)
            draft_dir = Path(drafts_root) / draft_name
            mac_media_dir = f"{mac_sync}/{draft_sub}/{draft_name}"

            for sec in ['videos', 'audios']:
                for item in d.get('materials', {}).get(sec, []):
                    if item.get('path'):
                        item['path'] = f"{mac_media_dir}/{Path(item['path']).name}"

            # ── Step C：确保 BaiduSyncdisk 草稿文件夹存在（不删！），写入文件 ──
            draft_dir.mkdir(parents=True, exist_ok=True)

            # 清理旧 BGM 文件（保留当前使用的媒体 + 草稿 JSON/元数据文件）
            keep_names = {Path(m).name for m in used_media if m}
            keep_suffixes = {'.json', '.jpg', '.tmp', '.bak'}
            for f in draft_dir.iterdir():
                if f.is_file() and f.suffix.lower() not in keep_suffixes and f.name not in keep_names:
                    f.unlink()
                    print(f"  清理旧文件: {f.name}")

            # 复制媒体文件
            for media in used_media:
                if media and Path(media).exists():
                    _shutil.copy2(media, draft_dir / Path(media).name)
                    print(f"  复制媒体: {Path(media).name}")

            # 最后写 draft_content.json（覆写，不删文件夹）
            draft_json_path = draft_dir / 'draft_content.json'
            with open(draft_json_path, 'w', encoding='utf-8') as f:
                _json.dump(d, f, ensure_ascii=False)
            print(f"  draft_content.json 已更新 ({len(d.get('materials',{}).get('audios',[]))} 条音频)")

            # ── Step D：更新 root_meta_info.json ──────────────────────────────
            mac_draft_root = f"{mac_sync}/{draft_sub}"
            root_meta_path = Path(drafts_root) / 'root_meta_info.json'
            if root_meta_path.exists():
                with open(root_meta_path, 'r', encoding='utf-8') as f:
                    root_meta = _json.load(f)
            else:
                root_meta = {"all_draft_store": [], "root_path": mac_draft_root}
            root_meta["root_path"] = mac_draft_root

            meta_path = draft_dir / 'draft_meta_info.json'
            draft_id = str(_uuid.uuid4()).upper()
            if meta_path.exists():
                with open(meta_path, 'r', encoding='utf-8') as f:
                    draft_id = _json.load(f).get('draft_id', draft_id)

            now_us = int(_time.time() * 1_000_000)
            dur_us = int(video_dur_s * 1_000_000)
            mac_draft_dir = f"{mac_sync}/{draft_sub}/{draft_name}"
            new_entry = {
                "draft_cloud_last_action_download": False,
                "draft_cloud_purchase_info": "", "draft_cloud_template_id": "",
                "draft_cloud_tutorial_info": "", "draft_cloud_videocut_purchase_info": "",
                "draft_cover": f"{mac_draft_dir}/draft_cover.jpg",
                "draft_fold_path": mac_draft_dir,
                "draft_id": draft_id,
                "draft_is_ai_shorts": False, "draft_is_invisible": False,
                "draft_json_file": f"{mac_draft_dir}/draft_content.json",
                "draft_name": draft_name, "draft_new_version": "",
                "draft_root_path": mac_draft_root,
                "draft_timeline_materials_size": 0, "draft_type": "",
                "tm_draft_cloud_completed": "", "tm_draft_cloud_modified": 0,
                "tm_draft_create": now_us, "tm_draft_modified": now_us,
                "tm_draft_removed": 0, "tm_duration": dur_us,
            }
            store = [e for e in root_meta.get("all_draft_store", [])
                     if e.get("draft_name") != draft_name]
            store.insert(0, new_entry)
            root_meta["all_draft_store"] = store
            with open(root_meta_path, 'w', encoding='utf-8') as f:
                _json.dump(root_meta, f, ensure_ascii=False)

            # ── Step E：写 draft_meta_info.json（剪映靠此识别单个草稿）──────────
            draft_meta = {
                "cloud_package_completed_time": "",
                "draft_cloud_capcut_purchase_info": "",
                "draft_cloud_last_action_download": False,
                "draft_cloud_materials": [],
                "draft_cloud_purchase_info": "",
                "draft_cloud_template_id": "",
                "draft_cloud_tutorial_info": "",
                "draft_cloud_videocut_purchase_info": "",
                "draft_cover": "draft_cover.jpg",
                "draft_deeplink_url": "",
                "draft_enterprise_info": {
                    "draft_enterprise_extra": "", "draft_enterprise_id": "",
                    "draft_enterprise_name": "", "enterprise_material": []
                },
                "draft_fold_path": mac_draft_dir,
                "draft_id": draft_id,
                "draft_is_ai_packaging_used": False,
                "draft_is_ai_shorts": False,
                "draft_is_ai_translate": False,
                "draft_is_article_video_draft": False,
                "draft_is_from_deeplink": "false",
                "draft_is_invisible": False,
                "draft_materials": [],
                "draft_materials_copied_info": [],
                "draft_name": draft_name,
                "draft_new_version": "",
                "draft_removable_storage_device": "",
                "draft_root_path": mac_draft_root,
                "draft_segment_extra_info": [],
                "draft_timeline_materials_size_": 0,
                "draft_type": "",
                "tm_draft_cloud_completed": "",
                "tm_draft_cloud_modified": 0,
                "tm_draft_create": now_us,
                "tm_draft_modified": now_us,
                "tm_draft_removed": 0,
                "tm_duration": dur_us,
            }
            with open(meta_path, 'w', encoding='utf-8') as f:
                _json.dump(draft_meta, f, ensure_ascii=False)

            print(f"  草稿已同步到百度网盘: {draft_dir}")
        else:
            print(f"  草稿已生成: {draft_name}")
        return True

    except Exception as e:
        print(f"  [剪映草稿失败]: {e}")
        import traceback
        traceback.print_exc()
        return False


# ── 主流程 ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="选曲 + 生成剪映草稿测试")
    parser.add_argument("--folder", required=True, help="视频集数文件夹，如 assets/太古凌霄：唯我独尊10")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--skip-draft", action="store_true", help="只选曲不生成草稿（调试用）")
    args = parser.parse_args()

    folder = Path(args.folder).resolve()
    if not folder.exists():
        print(f"[错误] 文件夹不存在: {folder}")
        sys.exit(1)

    config = load_config(args.config)
    api_key = config.get("kimi_api_key")
    if not api_key:
        print("[错误] config.json 中缺少 kimi_api_key")
        sys.exit(1)
    model = config.get("kimi_model", MODEL)
    system_prompt = load_skill_prompt(DIRECTOR_SKILL_PATH)

    # ── Step 1: 定位剧本 ────────────────────────────────────────────────────
    print("[1/4] 定位剧本...")
    episode_num, script_path = find_episode_script(folder)
    if not script_path:
        sys.exit(1)
    script_text = script_path.read_text(encoding="utf-8")
    print(f"      {script_path.name}  ({len(script_text)} 字)")

    # ── Step 2: 加载曲库 ────────────────────────────────────────────────────
    print("[2/4] 加载曲库...")
    music_root_abs = Path.cwd() / MUSIC_ROOT
    catalog = load_all_music_tags(music_root_abs)
    print(f"      共 {len(catalog)} 首曲目")
    catalog_text = format_catalog_for_prompt(catalog)

    # ── Step 3: Kimi 选曲 ───────────────────────────────────────────────────
    print("[3/4] 调用 Kimi 选曲...")
    # 提前加载 clips manifest（如有），把场景切换点告知 Kimi
    _pre_video = find_output_video(folder)
    _pre_manifest = find_clips_manifest(_pre_video) if _pre_video else None
    _pre_dur = get_video_duration_s(_pre_video) if _pre_video else 0.0
    scene_context = build_scene_change_context(_pre_manifest, _pre_dur) if _pre_manifest else ""
    if scene_context:
        print(f"      场景切换上下文已注入 Kimi 提示词")
    result = call_kimi_director(api_key, system_prompt, script_text, catalog_text, model,
                                scene_context=scene_context)
    if not result:
        print("[错误] 选曲失败")
        sys.exit(1)

    print(f"\n      基调: {result.get('episode_tone', '')}")
    print(f"      题材: {result.get('genre', '')}\n")

    selections = result.get("selections", [])
    for i, sel in enumerate(selections, 1):
        bgm = resolve_bgm_path(sel)
        status = "OK" if bgm else "文件不存在"
        print(f"      [{i}] {sel.get('usage')} | {sel.get('file')} | {status}")
        print(f"           {sel.get('scene_desc')}  ← {sel.get('reason')}")

    # 保存选曲结果
    out_json = folder / "music_selection.json"
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n      选曲结果已保存: {out_json.name}")

    if args.skip_draft:
        return

    # ── Step 4: 找视频 + 生成草稿 ──────────────────────────────────────────
    print("\n[4/4] 生成剪映草稿...")
    video_path = find_output_video(folder)
    if not video_path:
        sys.exit(1)
    print(f"      视频: {video_path.name}")

    # 收集所有有效 BGM（带 start_percent）
    valid_bgm = []
    for sel in selections:
        p = resolve_bgm_path(sel)
        if p:
            start_pct = float(sel.get("start_percent", -1))
            valid_bgm.append((p, start_pct, sel.get("usage", "")))

    if not valid_bgm:
        print("  [错误] 所有选曲文件均不存在，无法生成草稿")
        sys.exit(1)

    # 根据 start_percent 计算实际起始时间
    # 若 Kimi 没有返回 start_percent，回退到均分
    video_dur_s = get_video_duration_s(video_path)
    n = len(valid_bgm)
    has_pct = all(pct >= 0 for _, pct, _ in valid_bgm)
    bgm_list = []
    for i, (p, pct, label) in enumerate(valid_bgm):
        if has_pct:
            start_s = video_dur_s * pct / 100.0
        else:
            start_s = (video_dur_s / n) * i
        bgm_list.append((p, start_s, label))

    mode = "按剧情节点" if has_pct else "均分"
    timing_info = "  ".join(f"{label}@{int(s)}s" for _, s, label in bgm_list)
    print(f"      共 {n} 首 BGM，{mode}铺排，按响度+场景自动归一化")
    print(f"      {timing_info}\n")

    srt_path = find_srt(folder)
    if srt_path:
        print(f"      字幕: {srt_path.name}")
    else:
        print(f"      字幕: 未找到 SRT，跳过字幕轨道")

    clips_manifest = find_clips_manifest(video_path)
    if clips_manifest:
        print(f"      片段清单: {len(clips_manifest)} 个片段（剪映轨道将显示独立片段）")
    else:
        print(f"      片段清单: 未找到，视频轨道使用整个合并视频")

    draft_name = video_path.stem
    ok = create_draft_multi_bgm(video_path, bgm_list, draft_name, config,
                                bgm_volume=0.2, srt_path=srt_path,
                                clips_manifest=clips_manifest,
                                source_folder=folder)
    if ok:
        print(f"\n[完成] 草稿「{draft_name}」已生成并同步到百度网盘")
    else:
        print("\n[失败] 草稿生成失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
