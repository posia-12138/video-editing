#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
优化版 Qwen3-ASR 字幕生成
- 提升时间戳准确性
- Kimi API 对照剧本重新断句+纠错（利用停顿信息+说话人信息）
- Kimi 视频分析去除硬字幕重复
- ffmpeg 烧录字幕到视频
"""

import json
import re
import sys
import os
import base64
import subprocess
import torch
import requests
from pathlib import Path
from openai import OpenAI

# 添加 Qwen3-ASR 到 Python 路径
QWEN3_ASR_PATH = Path(__file__).parent.parent / "libs" / "Qwen3-ASR"
sys.path.insert(0, str(QWEN3_ASR_PATH))

from qwen_asr import Qwen3ASRModel


def format_timestamp(seconds):
    """将秒数转换为 SRT 时间戳格式"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def parse_script(script_path):
    """
    解析剧本，提取有序的 (说话人, 台词) 列表。
    格式：角色名（动作）：台词内容 或 【旁白】内容
    返回：[{'speaker': '王猛', 'line': '小子站住此路是我开留下买路财'}, ...]
    """
    lines = []
    if not Path(script_path).exists():
        return lines

    with open(script_path, 'r', encoding='utf-8') as f:
        content = f.read()

    all_matches = []

    # 匹配 "角色名（...）：台词" 或 "角色名：台词"
    dialogue_pattern = re.compile(r'^([^\s（(【△※\-#\d][^（(：:]{0,10}?)(?:（[^）]*）)?[：:]\s*(.+)$', re.MULTILINE)
    for m in dialogue_pattern.finditer(content):
        speaker = m.group(1).strip()
        line = m.group(2).strip()
        clean_line = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', line)
        if clean_line and len(speaker) <= 6:
            all_matches.append((m.start(), {'speaker': speaker, 'line': clean_line, 'raw': line}))

    # 匹配旁白：【旁白】内容（旁白也会在视频中被读出，需要纳入纠错参考）
    narration_pattern = re.compile(r'^【旁白】(.+)$', re.MULTILINE)
    for m in narration_pattern.finditer(content):
        narration = m.group(1).strip()
        clean_line = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', narration)
        if clean_line:
            all_matches.append((m.start(), {'speaker': '旁白', 'line': clean_line, 'raw': narration}))

    # 按在剧本中出现的位置排序，保持顺序
    all_matches.sort(key=lambda x: x[0])
    lines = [item for _, item in all_matches]

    return lines


def build_word_sequence(words):
    """
    构建带停顿标记的字序列，供 Kimi 断句使用。
    格式：小子[0.4s]站住[2.6s]此路是我开...
    停顿 >= 0.3s 才标注。
    """
    parts = []
    for i, word in enumerate(words):
        parts.append(word.text)
        if i < len(words) - 1:
            gap = words[i + 1].start_time - word.end_time
            if gap >= 0.3:
                parts.append(f"[{gap:.1f}s]")
    return "".join(parts)


def _lcs_ratio(a, b):
    """最长公共子序列长度 / max(len(a), len(b))，用于字幕相似度计算（对首字错误更鲁棒）。"""
    m, n = len(a), len(b)
    if m == 0 or n == 0:
        return 0.0
    prev = [0] * (n + 1)
    for i in range(m):
        curr = [0] * (n + 1)
        for j in range(n):
            if a[i] == b[j]:
                curr[j + 1] = prev[j] + 1
            else:
                curr[j + 1] = max(prev[j + 1], curr[j])
        prev = curr
    return prev[n] / max(m, n)


def _seq_match_ratio(haystack, needle):
    """needle 的各字符在 haystack 中能按序找到的比例（0.0~1.0）。"""
    j = 0
    matched = 0
    for ch in needle:
        while j < len(haystack) and haystack[j] != ch:
            j += 1
        if j < len(haystack):
            matched += 1
            j += 1
    return matched / max(len(needle), 1)


def _map_clean_pos_to_orig(seg, clean_pos):
    """把 cleaned（仅汉字）版本的字符位置映射回原始字符串的位置。"""
    count = 0
    for i, ch in enumerate(seg):
        if re.match(r'[\u4e00-\u9fff\u3400-\u4dbf]', ch):
            if count == clean_pos:
                return i
            count += 1
    return len(seg)


def split_merged_segments(segments, script_lines):
    """
    对照剧本检测并拆开被错误合并的字幕段落。
    如果某 segment 的后半部分清晰对应剧本中某行台词 l2，
    且前半部分也能对应 l2 的上一行台词 l1，则在边界处拆分。

    例：Kimi 输出 "这就是武道纳灵级无敌"，剧本有 ["这就是武道痛快", "纳灵级无敌"]
    → 搜索到 k=5 时 part2="纳灵级无敌" 与 l2 完美匹配，part1="这就是武道" 覆盖 l1 前半
    → 拆成 ["这就是武道", "纳灵级无敌"]
    """
    if not script_lines:
        return segments

    script_texts = [
        re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', sl['line'])
        for sl in script_lines
    ]

    result = []
    for seg in segments:
        if not seg.strip():
            result.append(seg)
            continue

        clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', seg)

        # 太短的不处理（避免误分割）
        if len(clean) < 5:
            result.append(seg)
            continue

        split_done = False
        for i in range(len(script_texts) - 1):
            l1 = script_texts[i]
            l2 = script_texts[i + 1]

            # 两行都不能太短（避免单字误匹配）
            if len(l1) < 2 or len(l2) < 2:
                continue
            # l2 不能比整个 segment 还长
            if len(l2) >= len(clean) - 1:
                continue

            # 搜索最佳分割点：找 l2 最匹配的 suffix 起始位置
            best_k = None
            best_score = 0.0

            for k in range(2, len(clean) - 1):
                part2 = clean[k:]
                part1 = clean[:k]

                # part2 长度须与 l2 长度接近（±2字），防止短 l2 在任意位置误匹配
                if abs(len(part2) - len(l2)) > 2:
                    continue

                # l2 与 part2 相似度：用 LCS 比例（对首字 ASR 错误更鲁棒）
                r2 = _lcs_ratio(part2, l2)
                if r2 < 0.75:
                    continue

                # l1 的字符有多少出现在 part1 中：
                # 用双向 max 允许 part1 是 l1 的片段（头部或尾部连续片段均可）
                r1 = max(_seq_match_ratio(l1, part1), _seq_match_ratio(part1, l1))
                if r1 < 0.3:
                    continue

                score = r1 + r2
                if score > best_score:
                    best_score = score
                    best_k = k

            if best_k is not None:
                orig_k = _map_clean_pos_to_orig(seg, best_k)
                seg1 = seg[:orig_k].strip()
                seg2 = seg[orig_k:].strip()
                if seg1 and seg2:
                    print(f"[SPLIT] Script-guided split: '{seg}' -> '{seg1}' | '{seg2}' "
                          f"(script L{i+1}+L{i+2}, score={best_score:.2f})")
                    result.append(seg1)
                    result.append(seg2)
                    split_done = True
                    break

        if not split_done:
            result.append(seg)

    return result


def fix_isolated_single_chars(segments, script_lines):
    """
    修复孤立单字段落（ASR完全误识的情况）。

    若某段落仅含1个汉字，且其后紧跟的多字段落能对应剧本第 L[k] 行，
    则将该单字替换为第 L[k-1] 行中尚未被前面段落覆盖的尾部词。

    典型案例：
      剧本 L[9]="这就是武道痛快"，L[10]="纳灵级无敌"
      segments: ..., "这就是", "武道", "他", "大灵级无敌", ...
      → "他" 后面的 "大灵级无敌" 对应 L[10]
      → L[9] 的尾部 "痛快" 尚未被 "这就是"+"武道" 覆盖
      → 将 "他" 替换为 "痛快"
    """
    if not script_lines:
        return segments

    script_texts = [
        re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', sl['line'])
        for sl in script_lines
    ]

    result = list(segments)

    for idx, seg in enumerate(segments):
        clean_seg = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', seg)
        if len(clean_seg) != 1:
            continue

        # 1. 向后找第一个多字段落，确定它对应哪行剧本
        next_script_idx = -1
        for nidx in range(idx + 1, min(len(segments), idx + 5)):
            nc = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', segments[nidx])
            if len(nc) < 2:
                continue
            # 第一个多字段落：找最佳剧本行匹配
            best_r, best_li = 0.0, -1
            for li, lt in enumerate(script_texts):
                r = _lcs_ratio(nc, lt)
                if r > best_r:
                    best_r, best_li = r, li
            if best_r >= 0.5:
                next_script_idx = best_li
            break  # 无论是否匹配成功，只看第一个多字段落

        if next_script_idx <= 0:
            continue

        # 2. 目标前行台词 = next 对应行的上一行
        target_line_idx = next_script_idx - 1
        L_prev = script_texts[target_line_idx]
        if not L_prev:
            continue

        # 3. 向前找与 L_prev 匹配的片段（宽松匹配，因为已知目标行）
        prev_matching_segs = []
        for pidx in range(idx - 1, max(-1, idx - 8), -1):
            pc = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', segments[pidx])
            if len(pc) < 2:
                continue
            if _lcs_ratio(pc, L_prev) >= 0.25:
                prev_matching_segs.append(pc)

        # 4. 计算 L_prev 中尚未被前面段落覆盖的尾部
        consumed_up_to = 0
        for seg_text in prev_matching_segs:
            pos = L_prev.find(seg_text)
            if pos >= 0:
                consumed_up_to = max(consumed_up_to, pos + len(seg_text))
        tail = L_prev[consumed_up_to:]

        # 只替换1-4字的短尾部，避免大段替换
        if 1 <= len(tail) <= 4:
            print(f"[CONTEXT_FIX] Single-char '{seg}' -> '{tail}' "
                  f"(uncovered tail of script L{target_line_idx + 1}: '{L_prev}')")
            result[idx] = tail

    return result


def fix_script_mismatches(sentences, script_lines, low_threshold=0.35, high_threshold=0.5, context_window=6):
    """
    修复 ASR 完全识别错误的多字段落（fix_isolated_single_chars 的多字扩展版）。

    当某字幕段落与所有剧本行的最佳 LCS 匹配分数低于 low_threshold 时，
    通过上下文（前后具有高置信度匹配的邻近段落）推断该段落应有的内容，
    用剧本中尚未被前面段落覆盖的尾部替换。

    典型案例：
      剧本 L[j]="再说一句捏碎你们"，L[j+1]="哟陆安躲了一礼拜敢来了"
      字幕: ..., "再说一句", "你碎了哟", "陆安", "躲了一礼拜敢来了", ...
      → "你碎了哟" 与所有剧本行匹配分数极低 (0.125)
      → "躲了一礼拜敢来了" 匹配 L[j+1]，next_script_idx = j+1
      → target_line = L[j] = "再说一句捏碎你们"
      → "再说一句" 已覆盖前4字，tail = "捏碎你们"
      → 将 "你碎了哟" 替换为 "捏碎你们"
    """
    if not script_lines:
        return sentences

    script_texts = [
        re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', sl['line'])
        for sl in script_lines
    ]

    def best_match(text):
        clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', text)
        if len(clean) < 2:
            return -1, 0.0
        best_r, best_li = 0.0, -1
        for li, lt in enumerate(script_texts):
            if not lt:
                continue
            r = _lcs_ratio(clean, lt)
            if r > best_r:
                best_r, best_li = r, li
        return best_li, best_r

    # 预计算所有句子的最佳剧本匹配分数
    matches = [best_match(s['text']) for s in sentences]

    result = list(sentences)

    for idx, sent in enumerate(sentences):
        li, score = matches[idx]
        clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', sent['text'])

        # 跳过：匹配度够高、单字（由 fix_isolated_single_chars 处理）、或空
        if score >= low_threshold or len(clean) <= 1:
            continue

        # 跳过：ASR 文本与某剧本行的 ASR 覆盖率（LCS/len_asr）>= 0.5
        # 表示 ASR 文本中 >50% 的字符在剧本中出现，是该行的合理识别片段，不应替换
        # 典型保护场景："前方主攻侧翼包击"（8字）vs "记住...前锋主攻侧翼包抄后卫防御"（20字）
        # → LCS=5（主攻侧翼包）, score=5/20=0.25（低于阈值但不代表识别失败）
        # → ASR覆盖率=5/8=62.5% → 不应替换
        _best_asr_coverage = 0.0
        for _lt in script_texts:
            if not _lt:
                continue
            _lcs_len = int(_lcs_ratio(clean, _lt) * max(len(clean), len(_lt)))
            _cov = _lcs_len / max(len(clean), 1)
            if _cov > _best_asr_coverage:
                _best_asr_coverage = _cov
        if _best_asr_coverage >= 0.5:
            continue

        # 1. 向后找高置信度段落（可跳过多个低置信度段落）
        next_script_idx = -1
        next_nidx = -1
        for nidx in range(idx + 1, min(len(sentences), idx + context_window + 1)):
            nli, nsc = matches[nidx]
            nc = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', sentences[nidx]['text'])
            if len(nc) < 2:
                continue
            if nsc >= high_threshold and nli >= 0:
                next_script_idx = nli
                next_nidx = nidx
                break

        if next_script_idx < 0:
            continue

        replacement = None

        # 候选 A：高置信度行的前一行的尾部（跨行情况，如 "你碎了哟"→"捏碎你们"）
        if next_script_idx > 0:
            target_idx = next_script_idx - 1
            L_target = script_texts[target_idx]
            if L_target:
                prev_matching = []
                for pidx in range(idx - 1, max(-1, idx - context_window - 1), -1):
                    pc = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', result[pidx]['text'])
                    if len(pc) < 2:
                        continue
                    if _lcs_ratio(pc, L_target) >= 0.25:
                        prev_matching.append(pc)

                consumed = 0
                for pt in prev_matching:
                    pos = L_target.find(pt)
                    if pos >= 0:
                        consumed = max(consumed, pos + len(pt))
                tail = L_target[consumed:]

                if 2 <= len(tail) <= 8 and tail != clean:
                    replacement = (tail, target_idx, f"tail of L{target_idx+1}: '{L_target}'")
                elif not prev_matching and 2 <= len(L_target) <= 8 and L_target != clean:
                    claimed = any(
                        matches[j][0] == target_idx and matches[j][1] >= high_threshold
                        for j in range(len(sentences)) if j != idx
                    )
                    if not claimed:
                        replacement = (L_target, target_idx, f"unclaimed L{target_idx+1}")

        # 候选 B：同一剧本行的前缀（同行情况，如 "你们"→"没实力"）
        # 当后面的高置信度段落只覆盖了该剧本行的后半，前半是失败段落应有的内容
        if replacement is None and next_nidx >= 0:
            L_same = script_texts[next_script_idx]
            if L_same:
                nc_text = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', sentences[next_nidx]['text'])
                pos = L_same.find(nc_text)
                if pos > 0:
                    prefix = L_same[:pos]
                    if 2 <= len(prefix) <= 8 and prefix != clean:
                        replacement = (prefix, next_script_idx, f"prefix of L{next_script_idx+1}: '{L_same}'")

        # 候选 C：下一句完整匹配一个极短剧本行（≤3字），当前句 ASR 完全失败 → 当前句也替换为该短行
        # 典型案例：ASR "放肆放手就"(38s) + 高置信 "找死"(56s) 完整匹配剧本 "找死"(L10)
        # → "放肆放手就" 应该也是 "找死"（同一台词在视频中出现两次）
        if replacement is None and next_nidx >= 0:
            L_same = script_texts[next_script_idx]
            if 1 <= len(L_same) <= 3:
                nc_text = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', sentences[next_nidx]['text'])
                # 下一句的文字完整匹配该短剧本行，且当前句比它长很多
                if nc_text == L_same and len(clean) > len(L_same) + 1 and L_same != clean:
                    replacement = (L_same, next_script_idx, f"short line repeat C{next_script_idx+1}: '{L_same}'")

        if replacement:
            new_text, new_li, reason = replacement
            print(f"[MISMATCH_FIX] '{sent['text']}' -> '{new_text}' "
                  f"(score={score:.2f}, {reason})")
            result[idx] = {**sent, 'text': new_text}
            matches[idx] = (new_li, 1.0)

    # 第一点五轮：子串匹配（SUBSTR_FIX）
    # 当 ASR 文本是某剧本行的精确子串（首尾各缺1-2个字），直接替换为完整剧本行
    # 典型案例："小姐跟你没" ⊂ "本小姐跟你没完" （首缺"本"，尾缺"完"）
    # 限制：子串长度 ≥ 4，剧本行不能超过 ASR 太多（最多多 4 字），防止过度扩展
    for idx, sent in enumerate(result):
        clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', sent['text'])
        if len(clean) < 4:
            continue
        li, score = matches[idx]
        if score >= 1.0:
            continue  # 已精确匹配，不需要
        for sli, L in enumerate(script_texts):
            if not L:
                continue
            if len(L) <= len(clean):
                continue  # 剧本行不比 ASR 长，不可能是父串
            if len(L) > len(clean) + 4:
                continue  # 剧本行比 ASR 长太多，避免扩展过度
            if clean in L:  # ASR 文本是剧本行的精确子串
                # 防护：剧本行是 ASR 文本的精确重复（如"公安来了"×2="公安来了公安来了"）
                # 音频时长通常只够说一遍，扩展到两倍会凭空加入剧本重复部分
                if L == clean * 2:
                    print(f"[SUBSTR_FIX SKIP] '{sent['text']}' skip expand to '{L}': "
                          f"script line is exact doubling of ASR text")
                    break
                # 防护：若扩展会增加一个前缀，而该前缀恰好就是相邻句子的文字，
                # 则跳过扩展（否则 DEDUP 会把相邻句作为"前缀片段"删掉，造成内容丢失）
                # 典型案例："一股召唤之力" ⊂ "我感应到一股召唤之力"，前句是 "我感应到"
                # → 扩展后 DEDUP_PREFIX_FRAG 会删除 "我感应到"，造成 "我感应到" 重复
                _pos_in_L = L.find(clean)
                _added_prefix = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', L[:_pos_in_L])
                if _added_prefix:
                    _neighbors = []
                    if idx > 0:
                        _neighbors.append(re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '',
                                                  result[idx - 1]['text']))
                    if idx < len(result) - 1:
                        _neighbors.append(re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '',
                                                  result[idx + 1]['text']))
                    if any(n == _added_prefix for n in _neighbors):
                        print(f"[SUBSTR_FIX SKIP] '{sent['text']}' skip expand to '{L}': "
                              f"prefix '{_added_prefix}' already exists as adjacent sentence")
                        break
                # 防护：目标剧本行已被另一句高置信度匹配占用，避免同一行被两句引用
                # 典型案例："小屁孩开什么玩笑"(65s)试图扩展到"你小屁孩开什么玩笑"，
                # 但该行已被"小屁孩开什么玩笑我表哥有拖拉机"(34s, score=0.53)占用
                _claimed = any(
                    matches[j][0] == sli and matches[j][1] >= high_threshold
                    for j in range(len(result)) if j != idx
                )
                if _claimed:
                    print(f"[SUBSTR_FIX SKIP] '{sent['text']}' skip expand to '{L}': "
                          f"L{sli+1} already claimed by another sentence")
                    break
                print(f"[SUBSTR_FIX] '{sent['text']}' -> '{L}' "
                      f"(substring match, L{sli+1}: '{L}')")
                result[idx] = {**sent, 'text': L}
                matches[idx] = (sli, 1.0)
                break

    # 第二轮：后缀精确匹配 → 替换不同的前缀
    # 适用场景：ASR 把前几个字完全说错，但后半段完全正确
    # 例：script="废物也配和天才走在一起"，ASR="呦我也没和天才走在一起" → 只替换前缀"呦我也没"→"废物也配"
    MIN_SUFFIX_LEN = 4  # 后缀至少4字才认为是可靠的匹配
    for idx, sent in enumerate(result):
        clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', sent['text'])
        if len(clean) < MIN_SUFFIX_LEN:  # 太短则不处理
            continue
        for li, L in enumerate(script_texts):
            if len(L) < MIN_SUFFIX_LEN + 1:
                continue
            # 找最长的公共后缀
            suffix_len = 0
            for k in range(1, min(len(clean), len(L)) + 1):
                if clean[-k] == L[-k]:
                    suffix_len = k
                else:
                    break
            if suffix_len < MIN_SUFFIX_LEN:
                continue
            # 检查前缀：script 的前缀 vs ASR 的前缀
            script_prefix = L[:-suffix_len] if suffix_len < len(L) else ''
            asr_prefix = clean[:-suffix_len] if suffix_len < len(clean) else ''
            # asr_prefix 为空说明 ASR 文本就是 script 的尾部子串（如 "约没说这个" ⊆ "契约没说这个"）
            # PREFIX_FIX: 若 script 前缀只差 1-2 字，补上缺失的前缀
            if not asr_prefix:
                script_prefix_to_add = L[:-suffix_len] if suffix_len < len(L) else ''
                if script_prefix_to_add and 1 <= len(script_prefix_to_add) <= 2:
                    new_text = script_prefix_to_add + clean
                    print(f"[PREFIX_FIX] '{sent['text']}' -> '{new_text}' "
                          f"(prefix='{script_prefix_to_add}' + suffix='{clean}', L{li+1}: '{L}')")
                    result[idx] = {**sent, 'text': new_text}
                    matches[idx] = (li, 1.0)
                    break
                continue
            if not script_prefix or script_prefix == asr_prefix:
                continue
            if 2 <= len(script_prefix) <= 8:
                print(f"[SUFFIX_FIX] '{sent['text']}' -> '{script_prefix + clean[-suffix_len:]}' "
                      f"(suffix='{clean[-suffix_len:]}' matched L{li+1}: '{L}')")
                result[idx] = {**sent, 'text': script_prefix + clean[-suffix_len:]}
                matches[idx] = (li, 1.0)
                break  # 找到一个匹配就够了

    # 第二·五轮：短句尾部精确匹配 → 剥除前导噪音字
    # 适用场景：ASR 仅捕获到剧本行末尾的 2-3 字，首字因音似被错误识别
    # 例：script="伟东外面好像有人喊救命"，ASR="德救命" → "德"≠"喊"，尾部"救命"精确匹配 → 截掉"德"→"救命"
    for idx, sent in enumerate(result):
        clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', sent['text'])
        if not (2 <= len(clean) <= 3):
            continue
        for li, L in enumerate(script_texts):
            if len(L) < len(clean) + 2:  # 剧本行必须比ASR长至少2字
                continue
            # 检查尾部精确匹配（至少2字）
            tail_len = 0
            for k in range(1, len(clean) + 1):
                if clean[-k] == L[-k]:
                    tail_len = k
                else:
                    break
            if tail_len < 2:
                continue
            # 前导部分（clean比tail多的字）不能完全匹配script对应位置
            leading = clean[:-tail_len] if tail_len < len(clean) else ''
            if not leading:
                continue  # ASR完全是script的尾巴，已由PREFIX_FIX处理
            # 确认前导字确实是噪音（不匹配script对应位置）
            script_pos = L[-(tail_len + len(leading)):-tail_len] if tail_len + len(leading) <= len(L) else ''
            if script_pos and leading == script_pos:
                continue  # 前导字匹配script，不是噪音
            tail_text = clean[-tail_len:]
            print(f"[SHORT_TAIL_FIX] '{sent['text']}' -> '{tail_text}' "
                  f"(stripped leading '{leading}', tail='{tail_text}' matched L{li+1}: '{L}')")
            result[idx] = {**sent, 'text': tail_text}
            matches[idx] = (li, tail_len / len(L))
            break

    # 第三轮：近精确匹配 → 替换1-2个不同的字
    # 适用场景：ASR 基本正确但某个字完全不同（非音似），整体 LCS ≥ 85%
    # 例：script="你惹他们没问题"，ASR="你说他们没问题" → 只有"说"vs"惹"不同
    # 也包含 TRIM_FIX：ASR 比 script 多一个尾字
    TRAILING_PARTICLES = set('吗呢吧啊哦嗯哈呀啦哟')
    for idx, sent in enumerate(result):
        clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', sent['text'])
        if len(clean) < 4:
            continue
        for li, L in enumerate(script_texts):
            if not L:
                continue
            # TRIM_FIX: ASR 比 script 多一个尾字，且去掉后完全一致（短行精确匹配）
            if len(clean) == len(L) + 1 and clean[:len(L)] == L:
                print(f"[TRIM_FIX] '{sent['text']}' -> '{L}' (trimmed trailing char '{clean[-1]}', L{li+1})")
                result[idx] = {**sent, 'text': L}
                matches[idx] = (li, 1.0)
                break
            # TRIM_FIX_PREFIX: ASR 首部多余1-2字，去掉后完全一致
            # 典型案例："好三块碎片" → "三块碎片"，"成了结束了" → "结束了"
            _trimmed_prefix = False
            for _n in range(1, 3):
                if len(clean) == len(L) + _n and clean[_n:] == L:
                    print(f"[TRIM_FIX_PREFIX] '{sent['text']}' -> '{L}' "
                          f"(trimmed {_n} leading char(s) '{clean[:_n]}', L{li+1})")
                    result[idx] = {**sent, 'text': L}
                    matches[idx] = (li, 1.0)
                    _trimmed_prefix = True
                    break
            if _trimmed_prefix:
                break
            # DEDUP_PREFIX: ASR 句首词语重复（口吃/回声），去掉第一次重复后精确匹配剧本
            # 典型案例："拼了拼了混沌五元灭" → "拼了混沌五元灭"
            #           "李霄李霄本座等你很久了" → "李霄本座等你很久了"
            if len(clean) >= 4:
                _dedup_found = False
                for _plen in range(1, min(5, len(clean) // 2 + 1)):
                    _p = clean[:_plen]
                    if clean[_plen:_plen * 2] == _p and clean[_plen:] == L:
                        print(f"[DEDUP_PREFIX] '{sent['text']}' -> '{L}' (removed dup prefix '{_p}', L{li+1})")
                        result[idx] = {**sent, 'text': L}
                        matches[idx] = (li, 1.0)
                        _dedup_found = True
                        break
                if _dedup_found:
                    break
            # TRIM_FIX_SUFFIX: ASR 末字多余，且去掉后恰好是某行剧本的精确尾缀
            # 典型案例："这是规矩契" → "这是规矩"（"这是规矩"是"夫妻当然要同住这是规矩"的尾缀）
            if len(clean) >= 4 and len(L) >= len(clean) and L.endswith(clean[:-1]):
                r_trimmed = _lcs_ratio(clean[:-1], L)
                r_full = _lcs_ratio(clean, L)
                if r_trimmed >= r_full:
                    trimmed = clean[:-1]
                    print(f"[TRIM_FIX_SUFFIX] '{sent['text']}' -> '{trimmed}' "
                          f"(suffix match L{li+1}: '{L}', score {r_full:.2f}→{r_trimmed:.2f})")
                    result[idx] = {**sent, 'text': trimmed}
                    matches[idx] = (li, r_trimmed)
                    break
            # NEAR_TRIM_FIX_SUFFIX: TRIM_FIX_SUFFIX 的宽松版：末字多余，且去掉后与 script 尾缀只差 ≤1 字
            # 典型案例："看谁赚的多我"(6字) → script"比赚钱一个月看谁赚得多"[-5:]="看谁赚得多"，diff=1
            # → 裁去末字"我"，并将"的"纠正为"得"（使用script尾缀文字）
            if len(clean) >= 4 and len(L) >= len(clean):
                _nts_trimmed = clean[:-1]
                _nts_L_tail = L[-len(_nts_trimmed):]
                if len(_nts_L_tail) == len(_nts_trimmed):
                    _nts_diffs = sum(a != b for a, b in zip(_nts_trimmed, _nts_L_tail))
                    if _nts_diffs <= 1:
                        _nts_r_trimmed = _lcs_ratio(_nts_trimmed, L)
                        _nts_r_full = _lcs_ratio(clean, L)
                        if _nts_r_trimmed >= _nts_r_full:
                            _nts_text = _nts_L_tail if _nts_diffs == 1 else _nts_trimmed
                            print(f"[NEAR_TRIM_FIX_SUFFIX] '{sent['text']}' -> '{_nts_text}' "
                                  f"(near-suffix, tail_diff={_nts_diffs}, L{li+1})")
                            result[idx] = {**sent, 'text': _nts_text}
                            matches[idx] = (li, _nts_r_trimmed)
                            break
            # NEAR_TRIM_SUFFIX: ASR 比 script 多1尾字，且去掉后与 script 只差 ≤1 字（如"的/得"混淆）
            # 典型案例："比赚钱一个月看谁赚的多我"(12字) vs 剧本"比赚钱一个月看谁赚得多"(11字)
            # → 裁去末字"我"，剩余与 script 仅差1字("的"→"得") → 直接使用 script 文字
            if len(clean) >= 5 and len(L) + 1 == len(clean):
                _trimmed = clean[:-1]
                _ndiffs = sum(a != b for a, b in zip(_trimmed, L))
                if _ndiffs <= 1:
                    print(f"[NEAR_TRIM_SUFFIX] '{sent['text']}' -> '{L}' "
                          f"(near-trim, diff={_ndiffs}, L{li+1})")
                    result[idx] = {**sent, 'text': L}
                    matches[idx] = (li, 1.0)
                    break
            # NEAR_FIX_LONGER: script 比 ASR 多恰好1个尾字，且去掉后与 ASR 只差 ≤1 个字
            # 典型案例："小哥我们来"(5字) vs "霄哥我们来了"(6字)
            # → ASR 漏了末字'了'，且第1字'小'音似'霄'，lcs_r=0.80 不足以触发下面的 NEAR_FIX
            # → 比较 clean(5字) vs L[:-1]="霄哥我们来"(5字)，diff=1 → 替换为完整剧本行
            if len(L) == len(clean) + 1:
                _L_trim = L[:-1]
                _diffs_longer = sum(1 for _a, _b in zip(clean, _L_trim) if _a != _b)
                if _diffs_longer <= 1:
                    print(f"[NEAR_FIX_LONGER] '{sent['text']}' -> '{L}' "
                          f"(diff={_diffs_longer} vs '{_L_trim}' + trailing '{L[-1]}', L{li+1})")
                    result[idx] = {**sent, 'text': L}
                    matches[idx] = (li, 1.0)
                    break

            # 尝试与 script 行（及去掉尾部语气词后）做同长比对
            L_cmp = L.rstrip(''.join(TRAILING_PARTICLES))
            if len(L_cmp) != len(clean):
                continue
            lcs_r = _lcs_ratio(clean, L_cmp)
            if lcs_r < 0.85:
                continue
            # 找出不同的位置
            diffs = [(i, clean[i], L_cmp[i]) for i in range(len(clean)) if clean[i] != L_cmp[i]]
            if 1 <= len(diffs) <= 2:
                print(f"[NEAR_FIX] '{sent['text']}' -> '{L_cmp}' "
                      f"(lcs={lcs_r:.2f}, diffs={diffs}, L{li+1})")
                result[idx] = {**sent, 'text': L_cmp}
                matches[idx] = (li, 1.0)
                break

    # 第三点五轮：短片段合并（SHORT_MERGE / BACKWARD_MERGE）
    # 当 ASR 片段 ≤3字且 script 匹配度低，尝试与相邻片段合并后再匹配
    # 前向（SHORT_MERGE）：短片段 + 下一片段，典型案例："我公" + "司还有事" → "我公司还有事"
    # 后向（BACKWARD_MERGE）：上一片段 + 短片段，典型案例："他硬闯还凶我无法" + "无天" → "他硬闯还凶我无法无天了"
    # 合并后若 LCS ≥ 0.9 则直接使用剧本行文本；否则使用拼接文本
    _short_merge_deleted: set = set()

    def _try_merge(a_clean, b_clean):
        """合并 a+b，返回 (best_li, best_score) 或 None"""
        merged = a_clean + b_clean
        if len(merged) < 4:
            return None
        bl, bs = -1, 0.0
        for mli, mL in enumerate(script_texts):
            if not mL:
                continue
            if merged == mL:
                return mli, 1.0
            r = _lcs_ratio(merged, mL)
            if r > bs:
                bs = r
                bl = mli
        return (bl, bs) if bs >= high_threshold else None

    for idx, sent in enumerate(result):
        if idx in _short_merge_deleted:
            continue
        clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', sent['text'])
        if len(clean) > 3:
            continue
        li, score = matches[idx]
        if score >= high_threshold:
            continue

        # 尝试前向合并：短片段 + 下一片段
        next_idx = idx + 1
        while next_idx < len(result) and next_idx in _short_merge_deleted:
            next_idx += 1
        if next_idx < len(result):
            next_sent = result[next_idx]
            # 时间间隔检查：两句间停顿超过 2s 则不合并（避免跨场景拼接，如"救援"[16s停顿]"天尊大人..."）
            _fwd_gap = next_sent['start_time'] - sent['end_time']
            if _fwd_gap <= 2.0:
                next_clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', next_sent['text'])
                res = _try_merge(clean, next_clean)
                if res:
                    best_li, best_score = res
                    merged_clean = clean + next_clean
                    merged_text = script_texts[best_li] if best_score >= 0.9 else merged_clean
                    print(f"[SHORT_MERGE] '{sent['text']}'+'{next_sent['text']}' -> '{merged_text}' "
                          f"(score={best_score:.2f}, L{best_li+1})")
                    result[idx] = {**sent, 'text': merged_text, 'end_time': next_sent['end_time']}
                    matches[idx] = (best_li, best_score)
                    _short_merge_deleted.add(next_idx)
                    continue
                # 备用：合并后去掉1-2个前导噪音字，若剩余部分是某剧本行的精确前缀则合并
                # 典型案例："你来"+"人啊" → "你来人啊" → 去掉"你" → "来人啊" ⊂ "来人啊救命有人耍流氓了"
                _prefix_trim_found = False
                for _trim in range(1, 3):
                    if _prefix_trim_found:
                        break
                    _candidate = (clean + next_clean)[_trim:]
                    if len(_candidate) < 2:
                        break
                    for _mli, _mL in enumerate(script_texts):
                        if _mL.startswith(_candidate) and len(_mL) > len(_candidate):
                            print(f"[SHORT_MERGE_PREFIX] '{sent['text']}'+'{next_sent['text']}' -> '{_candidate}' "
                                  f"(trimmed {_trim} char(s), prefix of L{_mli+1}: '{_mL}')")
                            result[idx] = {**sent, 'text': _candidate, 'end_time': next_sent['end_time']}
                            matches[idx] = (_mli, len(_candidate) / max(len(_candidate), len(_mL)))
                            _short_merge_deleted.add(next_idx)
                            _prefix_trim_found = True
                            break
                if _prefix_trim_found:
                    continue
            else:
                print(f"[SHORT_MERGE SKIP] '{sent['text']}'+'{next_sent['text']}' gap={_fwd_gap:.1f}s > 2s，跳过")

        # 尝试后向合并：上一片段 + 短片段（仅当合并后分数高于上一片段原有分数时才合并）
        prev_idx = idx - 1
        while prev_idx >= 0 and prev_idx in _short_merge_deleted:
            prev_idx -= 1
        if prev_idx >= 0:
            prev_sent = result[prev_idx]
            # 时间间隔检查
            _bwd_gap = sent['start_time'] - prev_sent['end_time']
            if _bwd_gap <= 2.0:
                prev_clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', prev_sent['text'])
                prev_li, prev_score = matches[prev_idx]
                res = _try_merge(prev_clean, clean)
                if res:
                    best_li, best_score = res
                    # 防护：短片段匹配的剧本位置早于前句（如"我能干" li=A < "三天就干完了" li=B）
                    # 说明短片段是前文台词的 ASR 残影/回声，后向合并会污染前句内容
                    _bwd_script_regress = (li >= 0 and prev_li >= 0 and li < prev_li)
                    if _bwd_script_regress:
                        print(f"[BACKWARD_MERGE SKIP] '{prev_sent['text']}'+'{sent['text']}' "
                              f"script regression (li={li} < prev_li={prev_li})")
                    elif best_score > prev_score:  # 仅当合并改善了匹配才执行
                        merged_clean = prev_clean + clean
                        merged_text = script_texts[best_li] if best_score >= 0.9 else merged_clean
                        print(f"[BACKWARD_MERGE] '{prev_sent['text']}'+'{sent['text']}' -> '{merged_text}' "
                              f"(score={best_score:.2f} > prev={prev_score:.2f}, L{best_li+1})")
                        result[prev_idx] = {**prev_sent, 'text': merged_text, 'end_time': sent['end_time']}
                        matches[prev_idx] = (best_li, best_score)
                        _short_merge_deleted.add(idx)
            else:
                print(f"[BACKWARD_MERGE SKIP] '{prev_sent['text']}'+'{sent['text']}' gap={_bwd_gap:.1f}s > 2s，跳过")

    # 删除被合并的片段（倒序删除以保持索引稳定）
    for del_idx in sorted(_short_merge_deleted, reverse=True):
        result.pop(del_idx)
        matches.pop(del_idx)

    # 第三点六轮：合并后的前缀清洗（POST_MERGE_PREFIX_CLEANUP）
    # SHORT_MERGE / BACKWARD_MERGE 可能新生成：
    # - 前缀多余："成了结束了" -> "结束了"
    # - 句首重复："拼了拼了混沌五元归一" -> "拼了混沌五元归一"
    for idx, sent in enumerate(result):
        clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', sent['text'])
        if len(clean) < 4:
            continue
        for li, L in enumerate(script_texts):
            if not L:
                continue
            _trimmed_prefix = False
            for _n in range(1, 4):
                if len(clean) == len(L) + _n and clean[_n:] == L:
                    print(f"[POST_MERGE_TRIM_FIX_PREFIX] '{sent['text']}' -> '{L}' "
                          f"(trimmed {_n} leading char(s) '{clean[:_n]}', L{li+1})")
                    result[idx] = {**sent, 'text': L}
                    matches[idx] = (li, 1.0)
                    _trimmed_prefix = True
                    break
            if _trimmed_prefix:
                break
            _dedup_found = False
            for _plen in range(1, min(5, len(clean) // 2 + 1)):
                _p = clean[:_plen]
                if clean[_plen:_plen * 2] == _p and clean[_plen:] == L:
                    print(f"[POST_MERGE_DEDUP_PREFIX] '{sent['text']}' -> '{L}' "
                          f"(removed dup prefix '{_p}', L{li+1})")
                    result[idx] = {**sent, 'text': L}
                    matches[idx] = (li, 1.0)
                    _dedup_found = True
                    break
            if _dedup_found:
                break
            # POST_MERGE_PREFIX_ADD：SHORT_MERGE 后缺少前缀字，从剧本行补上
            # 典型案例："卖废品赚了五块" ← 剧本"我卖废品赚了五块"[1:] == "卖废品赚了五块" → 补"我"
            _prefix_added = False
            for _plen in range(1, 3):
                if len(L) == len(clean) + _plen and L[_plen:] == clean:
                    print(f"[POST_MERGE_PREFIX_ADD] '{sent['text']}' -> '{L}' "
                          f"(added {_plen} leading char(s) '{L[:_plen]}', L{li+1})")
                    result[idx] = {**sent, 'text': L}
                    matches[idx] = (li, 1.0)
                    _prefix_added = True
                    break
            if _prefix_added:
                break

    # 第四轮：极短剧本行重复检测（候选 D）
    # 当 ASR 句比其最佳匹配剧本行长 ≥2.5x，且后续高置信句完整匹配一个极短行（≤3字），
    # 则当前句也替换为该短行（同一台词在视频中重复出现的情况）
    # 典型案例："放肆放手就" 的 best_match="放手"(L8,0.4)，后续"找死"匹配script L7(li-1) ← 最近
    # 注意：优先选脚本位置最接近 li-1 的候选，避免选到错误的短行（如更远的"什么"）
    for idx, sent in enumerate(result):
        clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', sent['text'])
        if len(clean) < 4:
            continue
        li, score = matches[idx]
        if score >= high_threshold:
            continue
        if li >= 0 and len(script_texts[li]) > 0:
            if len(clean) / len(script_texts[li]) < 2.5:
                continue
        # 收集所有候选，选脚本位置最接近 li-1 的那个
        best_cand = None
        best_dist = float('inf')
        for nidx in range(idx + 1, min(len(result), idx + context_window + 1)):
            nc_clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', result[nidx]['text'])
            nli, nsc = matches[nidx]
            if nsc < high_threshold or nli < 0:
                continue
            L_next = script_texts[nli]
            if 1 <= len(L_next) <= 3 and nc_clean == L_next and L_next != clean:
                dist = abs(nli - (li - 1))
                if dist < best_dist:
                    best_dist = dist
                    best_cand = (nidx, nli, L_next)
        if best_cand:
            nidx, nli, L_next = best_cand
            print(f"[MISMATCH_FIX] '{sent['text']}' -> '{L_next}' "
                  f"(score={score:.2f}, short repeat D{nli+1}: '{L_next}')")
            result[idx] = {**sent, 'text': L_next}
            matches[idx] = (nli, 1.0)

    # 第五轮：超长字幕拆分（SPLIT_LONG）
    # 当字幕持续时间 > 4s，ASR 可能把两句话（中间有呼吸声/停顿）识别成了一句
    # 尝试在剧本中找到两个连续行，使得文本可以被拆分匹配，然后按字符比例分配时间
    SPLIT_MAX_DURATION = 4.0
    splits_to_insert = []  # list of (idx, left_sent, right_sent, l_li, l_score, r_li, r_score)
    for idx, sent in enumerate(result):
        dur = sent['end_time'] - sent['start_time']
        if dur <= SPLIT_MAX_DURATION:
            continue
        clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', sent['text'])
        if len(clean) < 4:
            continue
        # 枚举所有可能的分割点，找最优的两行匹配
        # 对于极长片段（>6s），降低匹配阈值（0.5），因为群演呼喊等场景ASR噪声大
        SPLIT_SCORE_THRESHOLD = 0.5 if dur > 6.0 else 0.7
        best_split = None
        best_split_score = 0.0
        for k in range(2, len(clean) - 1):
            left = clean[:k]
            right = clean[k:]
            if len(left) < 2 or len(right) < 2:
                continue
            # 找左部分最佳匹配
            best_l_li, best_l_score = -1, 0.0
            for mli, mL in enumerate(script_texts):
                if not mL:
                    continue
                if left == mL:
                    best_l_li, best_l_score = mli, 1.0
                    break
                r = _lcs_ratio(left, mL)
                if r > best_l_score:
                    best_l_score = r
                    best_l_li = mli
            if best_l_score < SPLIT_SCORE_THRESHOLD:
                continue
            # 找右部分最佳匹配（脚本位置须在左部分之后）
            best_r_li, best_r_score = -1, 0.0
            for mli in range(best_l_li + 1, len(script_texts)):
                mL = script_texts[mli]
                if not mL:
                    continue
                if right == mL:
                    best_r_li, best_r_score = mli, 1.0
                    break
                r = _lcs_ratio(right, mL)
                if r > best_r_score:
                    best_r_score = r
                    best_r_li = mli
                # 补充检查：right 是某剧本行的前缀子串（如 "什么厂长不厂长的" ⊂ "什么厂长不厂长的以后就是亲家了"）
                # 使用固定分数0.75：足够触发拆分（≥阈值0.7），但低于use_script_threshold（0.95），
                # 确保最终使用ASR原文而非完整剧本行（避免把下半句也带进来）
                if len(right) >= 4 and mL.startswith(right) and len(mL) > len(right):
                    _prefix_score = 0.75
                    if _prefix_score > best_r_score:
                        best_r_score = _prefix_score
                        best_r_li = mli
            if best_r_score < SPLIT_SCORE_THRESHOLD:
                continue
            # 加权平均分
            combined = (best_l_score * len(left) + best_r_score * len(right)) / len(clean)
            if combined > best_split_score:
                best_split_score = combined
                best_split = (k, best_l_li, best_l_score, best_r_li, best_r_score)

        if best_split:
            k, l_li, l_score, r_li, r_score = best_split
            # 对于极长片段，匹配分数 >= 0.5 时直接用剧本文字（ASR噪声太大，剧本更可靠）
            use_script_threshold = 0.5 if dur > 6.0 else 0.95
            left_text  = script_texts[l_li] if l_score >= use_script_threshold else clean[:k]
            right_text = script_texts[r_li] if r_score >= use_script_threshold else clean[k:]
            # 左段：从原始开始时间按字符比例推进（覆盖左段发声范围）
            split_t = sent['start_time'] + (k / len(clean)) * dur
            # 右段：从原始结束时间往回推（右段发声在 segment 末尾），中间留空白
            # 语速估算：~6字/秒 → 每字 ~0.167s；若推算值早于 split_t 则退化为 split_t
            SPEECH_RATE = 1.0 / 6.0  # 秒/字
            right_dur_est = len(clean) - k  # right_chars
            right_start = max(split_t, sent['end_time'] - right_dur_est * SPEECH_RATE)
            left_sent  = {**sent, 'text': left_text,  'end_time':   split_t}
            right_sent = {**sent, 'text': right_text, 'start_time': right_start}
            print(f"[SPLIT_LONG] '{sent['text']}' ({dur:.1f}s) → "
                  f"'{left_text}'({l_score:.2f}) [{sent['start_time']:.2f}-{split_t:.2f}] + "
                  f"'{right_text}'({r_score:.2f}) [{right_start:.2f}-{sent['end_time']:.2f}]")
            splits_to_insert.append((idx, left_sent, right_sent, l_li, l_score, r_li, r_score))
    # 倒序插入，保持索引稳定
    for idx, left_sent, right_sent, l_li, l_score, r_li, r_score in reversed(splits_to_insert):
        result[idx] = left_sent
        result.insert(idx + 1, right_sent)
        matches[idx] = (l_li, l_score)
        matches.insert(idx + 1, (r_li, r_score))

    # 第六轮：重复句尾去重（DEDUP_SUFFIX）
    # 当前句是前一句的句尾重复，且剧本中该文本不作为独立台词出现时，删除
    dedup_deleted: set = set()
    for idx in range(1, len(result)):
        if idx in dedup_deleted:
            continue
        sent     = result[idx]
        prev_sent = result[idx - 1]
        clean     = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', sent['text'])
        prev_clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', prev_sent['text'])
        if len(clean) < 2:
            continue
        if not prev_clean.endswith(clean):
            continue
        # 剧本中若有完全一致的独立行，说明这句确实是一条单独台词，不去重
        if any(clean == L for L in script_texts if L):
            continue
        print(f"[DEDUP_SUFFIX] '{sent['text']}' 是前句 '{prev_sent['text']}' 的重复句尾，删除")
        dedup_deleted.add(idx)
    # DEDUP_PREFIX_FRAGMENT: 当前句是下一句的句头前缀片段，且剧本中不是独立台词 → 删除当前句
    # 典型案例："霄儿撑住" 后紧跟 "霄儿撑住师父带你走" → 删除前者
    for idx in range(len(result) - 1):
        if idx in dedup_deleted:
            continue
        sent      = result[idx]
        next_sent = result[idx + 1]
        if idx + 1 in dedup_deleted:
            continue
        clean      = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', sent['text'])
        next_clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', next_sent['text'])
        if len(clean) < 2:
            continue
        _is_prefix = next_clean.startswith(clean)
        if not _is_prefix:
            # 模糊前缀检查：允许 ≤1 个字不同（如同音字纠错导致"天君听令"≈"天军听令随我杀敌"的前缀）
            # 条件：curr 长度 ≤ next 长度，且前 len(curr) 个字中差异 ≤ 1
            if len(clean) <= len(next_clean):
                _diffs = sum(1 for a, b in zip(clean, next_clean[:len(clean)]) if a != b)
                _is_prefix = (_diffs <= 1)
        if not _is_prefix:
            continue
        if any(clean == L for L in script_texts if L):
            continue
        print(f"[DEDUP_PREFIX_FRAG] '{sent['text']}' 是后句 '{next_sent['text']}' 的句头前缀（≤1字差异），删除")
        dedup_deleted.add(idx)
    for del_idx in sorted(dedup_deleted, reverse=True):
        result.pop(del_idx)
        matches.pop(del_idx)

    # 第七轮：剧本顺序校验（ORDER_CHECK）
    # 检测 ASR 幻觉：短句匹配到的剧本行比紧接下一句更靠后，
    # 说明该句"跳跃"到了剧本后段，很可能是误识别（如喊声被听成"不可能"）
    # 判定条件（同时满足）：
    #   1. 句子长度 ≤ 5 字
    #   2. 当前句的 script_index 比下一句的 script_index 超前 ≥ ORDER_JUMP 行
    #   3. 当前句的 script_index 比前一句的 script_index 超前 ≥ ORDER_JUMP 行
    #      （排除正常的快速推进：下一句只是相对靠前，而当前句本身不突兀）
    ORDER_JUMP = 5   # 超前多少行触发检测
    MAX_ORDER_LEN = 7  # 只对短句做校验
    order_deleted: set = set()
    for idx in range(len(result)):
        if idx in order_deleted:
            continue
        li, score = matches[idx]
        if li < 0:
            continue
        clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', result[idx]['text'])
        if len(clean) > MAX_ORDER_LEN:
            continue
        # 找前一个有效句的 script index
        prev_li = -1
        for pi in range(idx - 1, -1, -1):
            if pi not in order_deleted and matches[pi][0] >= 0:
                prev_li = matches[pi][0]
                break
        # 找后一个有效句的 script index
        next_li = -1
        for ni in range(idx + 1, len(result)):
            if ni not in order_deleted and matches[ni][0] >= 0:
                next_li = matches[ni][0]
                break
        # 当前句比后一句超前 ORDER_JUMP 行，且比前一句也超前 ORDER_JUMP 行
        jumped_over_next = (next_li >= 0 and li > next_li + ORDER_JUMP)
        jumped_from_prev = (prev_li < 0 or li > prev_li + ORDER_JUMP)
        if jumped_over_next and jumped_from_prev:
            print(f"[ORDER_CHECK] 删除 '{result[idx]['text']}' "
                  f"(script L{li+1}，前句 L{prev_li+1}，后句 L{next_li+1}，疑似幻觉)")
            order_deleted.add(idx)
    for del_idx in sorted(order_deleted, reverse=True):
        result.pop(del_idx)
        matches.pop(del_idx)

    return result


def fix_cross_segment_overlap(segments):
    """
    修复 Kimi 输出中相邻段落的文字重叠（上一句结尾 == 下一句开头）。

    典型案例：
      segment[i-1] = "第三关回答我何为冰"
      segment[i]   = "何为冰冰者至寒至坚宁折不弯"
      → "何为冰" 是前句末尾，也是后句开头 → 重叠3字
      → 修复后 segment[i] = "冰者至寒至坚宁折不弯"

    注意：只处理纯汉字部分的重叠；只在 overlap >= 1 且修复后段落不为空时生效。
    最大重叠长度限制为 min(len(prev), len(curr)-1)，确保不会把整个后句删掉。
    """
    result = list(segments)
    for i in range(1, len(result)):
        prev_clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', result[i - 1])
        curr_clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', result[i])
        if not prev_clean or not curr_clean:
            continue
        # 最多检测 min(len(prev), len(curr)-1) 个字的重叠，防止把整句删掉
        max_overlap = min(len(prev_clean), len(curr_clean) - 1)
        for overlap_len in range(max_overlap, 0, -1):
            if prev_clean[-overlap_len:] == curr_clean[:overlap_len]:
                new_curr = curr_clean[overlap_len:]
                if new_curr:
                    print(f"[CROSS_OVERLAP_FIX] '{result[i]}' -> '{new_curr}' "
                          f"(removed overlap '{curr_clean[:overlap_len]}' shared with prev)")
                    result[i] = new_curr
                break
    return result


def fix_segment_prefix_repetition(segments):
    """
    修复段落内开头词语重复（如 "放心放心他暂时退走了" → "放心他暂时退走了"）。

    检测规则：若段落开头 N 个字（N=1~4）与紧接其后的 N 个字完全相同，
    则去掉第一次出现的重复部分。

    仅处理 >= 5 字的段落，避免对短句误操作。
    """
    result = list(segments)
    for i, seg in enumerate(result):
        clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', seg)
        if len(clean) < 5:
            continue
        fixed = False
        for plen in range(1, min(5, len(clean) // 2 + 1)):
            prefix = clean[:plen]
            if clean[plen:plen * 2] == prefix:
                new_text = clean[plen:]
                print(f"[PREFIX_REPEAT_FIX] '{seg}' -> '{new_text}' "
                      f"(removed dup prefix '{prefix}')")
                result[i] = new_text
                fixed = True
                break
        _ = fixed  # 仅用于触发 break 后不继续外层循环
    return result


def kimi_resegment(words, api_key, script_path=None,
                   api_url='https://api.moonshot.cn/v1/chat/completions',
                   api_model='moonshot-v1-32k'):
    """
    使用 Kimi API 对照剧本重新断句（第一步：只断句，不纠错）。

    - 把字级时间戳序列（含停顿标记）+ 剧本台词顺序发给 Kimi
    - Kimi 根据停顿 + 语义 + 剧本台词顺序重新断句
    - 严格要求输出原始 ASR 文字，不做任何纠错
    - 换人说话时必须断句
    - 每句输出一行，不加标点，不加序号
    - 纯语气词（嗯/啊/哦等）输出空行
    - 返回文字段落列表（含空行）
    """
    print("\n[KIMI] Re-segmenting with Kimi API (step 1: segmentation only)...")

    # 构建带停顿标记的字序列
    word_seq = build_word_sequence(words)
    print(f"   Word sequence: {word_seq}")

    # 解析剧本台词顺序
    script_lines = []
    if script_path:
        script_lines = parse_script(script_path)
        print(f"   Script lines parsed: {len(script_lines)}")
        for sl in script_lines:
            print(f"      {sl['speaker']}: {sl['raw']}")

    # 构建剧本台词参考部分（仅用于判断说话人切换位置，不用于纠错）
    # 同时生成换人边界提示：相邻两行说话人不同时，标记为必须断句的边界
    if script_lines:
        script_ref = "\n".join(
            f"{sl['speaker']}: {sl['line']}" for sl in script_lines
        )
        # 生成换人边界列表：A的最后几个字 | B的开头几个字（取更多字，提高匹配精度）
        speaker_breaks = []
        for i in range(1, len(script_lines)):
            prev = script_lines[i - 1]
            curr = script_lines[i]
            if prev['speaker'] != curr['speaker']:
                # 取上一句末尾4个字 + 下一句开头4个字作为边界标记（更多字提高匹配精度）
                prev_tail = prev['line'][-4:] if len(prev['line']) >= 4 else prev['line']
                curr_head = curr['line'][:4] if len(curr['line']) >= 4 else curr['line']
                # 去掉标点符号，只保留汉字
                import re as _re
                prev_tail_clean = _re.sub(r'[^\u4e00-\u9fff]', '', prev_tail)
                curr_head_clean = _re.sub(r'[^\u4e00-\u9fff]', '', curr_head)
                speaker_breaks.append(
                    f"  「{prev['speaker']}」的台词结束于「{prev_tail_clean}」，"
                    f"「{curr['speaker']}」的台词开始于「{curr_head_clean}」——"
                    f"在识别序列中找到「{prev_tail_clean[-2:]}」之后、「{curr_head_clean[:2]}」之前的位置必须断句"
                )
        breaks_section = ""
        if speaker_breaks:
            breaks_section = "\n【换人断句边界（必须在这些位置断开，无论停顿大小）】：\n" + "\n".join(speaker_breaks) + "\n"

        script_section = f"""
剧本台词顺序（仅用于判断说话人切换位置，不要用剧本文字替换识别结果）：
{script_ref}
{breaks_section}
"""
    else:
        script_section = ""

    # 构建原始 ASR 文字序列（去掉停顿标记，供 Kimi 参考）
    raw_asr = "".join(w.text for w in words)

    prompt = f"""以下是语音识别结果，格式为：识别到的字 + [Xs] 表示停顿X秒。
{script_section}
【任务】对语音识别结果进行断句。严禁修改任何文字，只做分句。

【断句规则】
- 停顿 >= 0.5s 的位置通常是句子边界，需要断句
- 停顿 >= 2.0s 时，必须在此处断句，禁止将两侧内容合并在同一行
- 停顿 >= 5.0s 时，此处是场景切换或长时静音，两侧内容绝对不能合并
- 参考剧本中的说话人切换和标点符号位置进行断句
- 不要过度拆分（如"这是陷害"不能拆成"这""是""陷""害"）
- 每句建议 3~15 字

【输出格式】
- 每句一行，不加序号，不加标点符号，不加说话人名字
- 纯语气词（啊/哦/嗯）输出空行
- 输出的文字必须和输入完全一致，不能改字、不能补字、不能删字

【示例】
输入：你是谁[4.8s]老婆[0.9s]昨晚你可不是这么叫的[0.6s]的[7.0s]你叫我什么
输出：
你是谁
老婆昨晚你可不是这么叫的
你叫我什么

输入：来吧[13.5s]让你们见识一下
输出：
来吧
让你们见识一下

【禁止】
- 禁止纠正错别字（错别字纠正在后续步骤处理）
- 禁止替换专有名词
- 禁止添加或删除任何文字
- 禁止将有 >= 2秒 停顿分隔的内容合并到同一行（如"来吧[13.5s]让你们见识一下"必须断成两行）
- 不能把"喂好吃不贵"改成"啊你是谁"
- 如果识别内容和剧本差异很大，说明视频内容和剧本不匹配，必须保留识别内容

原始识别文字：{raw_asr}

语音识别序列（含停顿标记）：
{word_seq}

请输出断句后的字幕（每句一行，文字必须与输入完全一致，不得修改任何字）："""

    import time
    for _attempt in range(3):
        try:
            response = requests.post(
                api_url,
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json'
                },
                json={
                    'model': api_model,
                    'messages': [
                        {
                            'role': 'system',
                            'content': (
                                '你是专业的字幕断句助手。'
                                '你的任务是：根据停顿标记（[Xs]）和剧本台词顺序，把语音识别序列切分成若干句子。'
                                '【输出格式】每句一行，不加序号，不加标点符号，不加说话人名字。'
                                '【断句规则】'
                                '1. 停顿 >= 0.5s 的位置通常是句子边界'
                                '2. 换人说话时必须断句'
                                '3. 每句建议 3~15 字'
                                '4. 纯语气词（啊/哦/嗯）输出空行'
                                '【重要】只断句，不修改文字内容。停顿 >= 2s 必须断句，禁止合并大停顿两侧的内容。'
                            )
                        },
                        {'role': 'user', 'content': prompt}
                    ],
                    'temperature': 0.1,
                },
                timeout=60
            )
        except requests.exceptions.Timeout:
            print("[WARNING] Kimi API timeout")
            return None
        except Exception as e:
            print(f"[WARNING] Kimi API error: {e}")
            import traceback
            traceback.print_exc()
            return None

        if response.status_code == 429:
            wait = 10 * (2 ** _attempt)
            print(f"[WARNING] Kimi API error {response.status_code}: {response.text}")
            print(f"[RETRY] 等待 {wait}s 后重试（{_attempt+1}/3）...")
            time.sleep(wait)
            continue

        if response.status_code != 200:
            print(f"[WARNING] Kimi API error {response.status_code}: {response.text}")
            return None

        content = response.json()['choices'][0]['message']['content'].strip()
        print(f"\n[KIMI RAW OUTPUT]:\n{content}\n")

        # 解析输出行
        raw_lines = content.split('\n')
        segments = []
        for line in raw_lines:
            line = line.strip()
            # 移除序号（如 "1. " 或 "1、"）
            line = re.sub(r'^\d+[.、]\s*', '', line)
            # 移除停顿标记（如果 Kimi 误带入）
            line = re.sub(r'\[\d+\.\d+s\]', '', line).strip()
            # 移除说话人前缀（如 "王猛：" 或 "李霄: "）
            line = re.sub(r'^[\u4e00-\u9fff]{1,6}[：:]\s*', '', line).strip()
            segments.append(line)

        # 去掉末尾多余空行
        while segments and not segments[-1]:
            segments.pop()

        print(f"[KIMI] Parsed {len(segments)} segments:")
        for i, seg in enumerate(segments):
            print(f"   {i+1}. '{seg}'")

        # 验证字符总量：Kimi 丢失超过 20% 的字符时，说明违规删除了内容，拒绝并 fallback
        asr_char_count = sum(len(w.text) for w in words)
        seg_char_count = sum(len(re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', s)) for s in segments if s.strip())
        char_ratio = seg_char_count / max(asr_char_count, 1)
        if char_ratio < 0.80:
            print(f"[WARNING] Kimi dropped too much content: ASR={asr_char_count}chars, "
                  f"segments={seg_char_count}chars (ratio={char_ratio:.1%}). Discarding, fallback.")
            return None
        elif char_ratio < 0.90:
            print(f"[WARNING] Kimi may have dropped some content: ASR={asr_char_count}chars, "
                  f"segments={seg_char_count}chars (ratio={char_ratio:.1%})")

        # 对照剧本：检测并拆开被错误合并的段落（如 "这就是武道纳灵级无敌" -> 两行）
        if script_lines:
            segments = split_merged_segments(segments, script_lines)

        # 对照剧本：修复孤立单字段落（ASR完全误识，如 "他" 应为 "痛快"）
        if script_lines:
            segments = fix_isolated_single_chars(segments, script_lines)

        # 修复相邻段落文字重叠（Kimi 把上一句末尾重复写到下一句开头）
        segments = fix_cross_segment_overlap(segments)

        # 修复段落内开头词语重复（如 "放心放心他暂时退走了" → "放心他暂时退走了"）
        segments = fix_segment_prefix_repetition(segments)

        return segments

    return None  # 全部重试失败


def kimi_correct_sentences(sentences, api_key, script_path,
                           api_url='https://api.moonshot.cn/v1/chat/completions',
                           api_model='moonshot-v1-32k'):
    """
    使用 Kimi API 对照剧本纠正字幕文字（第二步：只纠错，不改时间戳）。

    策略：让 Kimi 输出替换词表（错误词→正确词），然后在代码里做全局替换。
    避免逐行输出导致的行数不匹配问题。
    """
    if not script_path or not sentences:
        return sentences

    script_lines = parse_script(script_path)
    if not script_lines:
        return sentences

    print("\n[KIMI] Building correction table from script (step 2: correction only)...")

    script_ref = "\n".join(f"{sl['speaker']}: {sl['raw']}" for sl in script_lines)
    # 所有剧本文字拼在一起（纯汉字），用于代码侧验证"正确词"是否在剧本中出现
    all_script_text = "".join(sl['line'] for sl in script_lines)
    # 把所有字幕文字拼成一段，供 Kimi 找错别字
    all_text = " | ".join(s['text'] for s in sentences)

    prompt = f"""你是专业的字幕纠错助手。以下是语音识别的字幕文字，可能含有错别字或专有名词错误。请对照剧本台词，找出需要纠正的词语。

剧本台词（含旁白，专有名词以此为准）：
{script_ref}

语音识别字幕（用 | 分隔各句）：
{all_text}

【剧本断句案例】：
剧本原文：
朱元璋（蹲下身）：标儿？你有啥办法？

朱标（眨眼）：爹，咱们玩个抓人的游戏好不好？

朱元璋（愣住）：抓人？

朱标：儿子派人"请"他来。天下人只会说世子孝顺！

从剧本可以看出正确的断句应该是：
- "标儿你有啥办法" 是一句（朱元璋说的）
- "爹咱们玩个抓人的游戏好不好" 是一句（朱标说的）
- "抓人" 是一句（朱元璋说的）
- "儿子派人请他来天下人只会说世子孝顺" 是一句（朱标说的）

但ASR识别出的字幕是：
- "彪儿你有啥办法爹" （错误：把下一句的开头"爹"接在了上一句结尾，且"标儿"错写成"彪儿"）
- "咱们玩个抓人的游戏好不好抓人儿" （错误：把下一句"抓人"接在了上一句结尾，且多了个"儿"）
- "子派人请他来" （错误：缺少开头的"儿"字）
- "天下人只会说世子孝顺啊" （"啊"为语气词删除）

应该纠正为：
- "彪儿你有啥办法爹"→"标儿你有啥办法" （纠正人名+去掉下一句开头的"爹"）
- "咱们玩个抓人的游戏好不好抓人儿"→"咱们玩个抓人的游戏好不好" （去掉下一句的"抓人儿"）
- “抓人”（朱元璋说的单独一句）
- "子派人请他来"→"儿子派人请他来" （补充缺失的"儿"字）
- "天下人只会说世子孝顺啊"→"天下人只会说世子孝顺" （去掉多余的"啊"）

请找出字幕中的错别字，输出替换词表，格式为每行一条：
错误词→正确词

【严格规则】：
1. 【只纠正同音字和错别字】：只纠正明显的同音字替换（如"之语"→"知鱼"）、错别字（如"林木"→"林默"、"彪儿"→"标儿"），不要替换整句话或调整台词顺序
2. 【不要替换完整台词】：如果字幕中的某句话和剧本中的某句话不一致，但意思相近或可能是演员临场发挥，保留字幕原文，不要替换
3. 【专有名词优先】：人名、地名等专有名词必须纠正（如"吴曼丽"→"胡曼丽"、"彪儿"→"标儿"）
4. 【允许小范围断句调整】：如果字幕把下一句的开头字错误地接在上一句结尾（参考上面的案例），可以去掉多余的字，或补充缺失的字
5. 【字数限制】：每条替换的"错误词"必须至少2个字，不超过6个字。"正确词"的字数可以比"错误词"少1-2个字（去掉多余的字），或多1个字（补充缺失的字）
6. 【必须实际出现】：每条替换的"错误词"必须在字幕中实际出现
7. 【仔细对照剧本】：仔细对照剧本中的人名、地名等专有名词，以及对话的断句位置
8. 【保守原则】：不确定的不要列出，宁可漏掉也不要误改；剧本中找不到的词不要作为"正确词"
9. 【无需纠正时】：如果没有需要纠正的词，输出"无"
10. 【只修正音似字】：只修正读音相近的字（音似错别字）。不要替换两个读音完全不同的词（如"刘皇叔"→"朱元璋"是错误的，因为这两个词读音不同）
11. 【禁止位置对应】：不要因为字幕第N句和剧本第N行"位置相近"就做替换。每条替换必须满足：错误词和正确词的拼音读音相同或极其相似（音似/同音），不能仅靠语义关联或出现顺序来猜测。如果找不到发音相似的对应词，就不要替换，直接输出"无"。
12. 【禁止调整词序】：演员说话时词序可能和剧本不同，这是正常现象。只纠正音似错字，不要调整词序来匹配剧本顺序。例如剧本写"天赋一星"，演员说的是"一星天赋"，ASR识别为"异星天赋"，则只纠正音似字"异→一"，输出"异星天赋→一星天赋"，而不是"异星天赋→天赋一星"。
13. 【人名错置例外】：若字幕某段的后半部分与剧本台词高度吻合，但前缀被ASR识别成了错误的人名（或感叹词+人名），而剧本中该位置的前缀是另一个人名，则可以直接替换前缀（即使发音不同）。判断标准：字幕后半与剧本后半完全一致，只有人名/前缀不同。例如：剧本"被刘少打了一顿就不敢来了"，字幕"陆安打了一顿就不敢来了"，则"陆安"→"被刘少"（因为"打了一顿就不敢来了"完全匹配，说明ASR把"被刘少"误识成了"陆安"）。

【反例警告】：
❌ 不要做："三百块服务费"→"青城第一美女" （这是替换整句话，违反规则1）
❌ 不要做："但敢点我林默的人也是第一个"→"一个你惹不起的" （这是替换整句话）
❌ 不要做："南海巨蟒化蛟"→"天赋一星" （发音完全不同，属于位置对应错误，违反规则11）
❌ 不要做："异星天赋"→"天赋一星" （不能为匹配剧本词序而调整词序，违反规则12）
✅ 可以做："异星天赋"→"一星天赋" （只纠正"异"→"一"的音似错字，保持词序不变）
✅ 可以做："林木"→"林默" （这是纠正专有名词错别字）
✅ 可以做："之语"→"知鱼" （这是纠正同音字）
✅ 可以做："吴曼丽"→"胡曼丽" （这是纠正专有名词）
✅ 可以做："彪儿"→"标儿" （这是纠正人名错别字）
✅ 可以做："彪儿你有啥办法爹"→"标儿你有啥办法" （纠正人名+去掉下一句开头的"爹"）
✅ 可以做："咱们玩个抓人的游戏好不好抓人儿"→"咱们玩个抓人的游戏好不好" （去掉下一句的"抓人儿"）
✅ 可以做："子派人请他来"→"儿子派人请他来" （补充缺失的"儿"字）
✅ 可以做："天下人只会说世子孝顺啊"→"天下人只会说世子孝顺" （去掉多余的"啊"）
✅ 可以做："陆安打了一顿"→"被刘少打了一顿" （人名错置规则13：剧本对应台词为"被刘少打了一顿就不敢来了"，"打了一顿就不敢来了"后半段完全吻合，前缀"陆安"被ASR认错为"被刘少"，替换前缀部分4→5字符符合字数规则）

替换词表："""

    import time
    for _attempt in range(3):
        try:
            response = requests.post(
                api_url,
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json'
                },
                json={
                    'model': api_model,
                    'messages': [
                        {
                            'role': 'system',
                            'content': (
                                '你是专业的字幕纠错助手。'
                                '根据剧本台词找出字幕中的错别字和专有名词错误，输出替换词表。'
                                '只列出确定需要纠正的词，不确定的不要列出。'
                            )
                        },
                        {'role': 'user', 'content': prompt}
                    ],
                    'temperature': 0.1,
                },
                timeout=60
            )
        except Exception as e:
            print(f"[WARNING] Kimi correction error: {e}")
            return sentences

        if response.status_code == 429:
            wait = 10 * (2 ** _attempt)
            print(f"[WARNING] Kimi correction API error {response.status_code}")
            print(f"[RETRY] 等待 {wait}s 后重试（{_attempt+1}/3）...")
            time.sleep(wait)
            continue

        if response.status_code != 200:
            print(f"[WARNING] Kimi correction API error {response.status_code}")
            return sentences

        content = response.json()['choices'][0]['message']['content'].strip()
        print(f"   Correction table:\n{content}")

        if content.strip() == '无' or not content.strip():
            print("   No corrections needed")
            return sentences

        # 解析替换词表
        replacements = {}
        for line in content.split('\n'):
            line = line.strip()
            # 支持 →、-> 或 " | " 分隔符（Kimi 有时用 | 代替 →）
            m = re.match(r'^(.+?)(?:→|->|\s+\|\s+)(.+)$', line)
            if m:
                wrong = m.group(1).strip()
                correct = m.group(2).strip()
                if wrong and correct and wrong != correct:
                    # 过滤单字替换：单字替换误伤率太高（如"好→啊"会破坏所有含"好"的句子）
                    # 例外：若错误字在整个剧本中完全找不到，说明它是纯音似错字，可以安全替换
                    # 例："冰" 在武道剧本中不存在，但"叮"在剧本中有，则允许 "冰→叮"
                    if len(wrong) < 2:
                        wrong_ch_single = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', wrong)
                        correct_ch_single = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', correct)
                        # 单字替换：wrong 和 correct 都必须是单字（防止 "颖"→"杀你" 这类 1字→多字 的非音似替换）
                        if wrong_ch_single and wrong_ch_single not in all_script_text \
                                and correct_ch_single and len(correct_ch_single) == 1 \
                                and correct_ch_single in all_script_text:
                            pass  # 允许：1字→1字，错字不在剧本，正确字在剧本
                        else:
                            print(f"   [SKIP] Single-char replacement filtered: '{wrong}' -> '{correct}'")
                            continue
                    # 过滤整句替换：错误词超过9字说明Kimi在替换整句话（如把台词内容替换成角色名）
                    if len(wrong) > 9:
                        print(f"   [SKIP] Sentence-level replacement filtered: '{wrong}' -> '{correct}' (wrong len={len(wrong)})")
                        continue
                    # 过滤过度替换：正确词字数不能比错误词多超过1个字
                    # 防止 Kimi 把两句话合并（如"混沌灵根..."→"混沌灵根...难怪能激活遗迹"）
                    if len(correct) > len(wrong) + 1:
                        print(f"   [SKIP] Over-replacement filtered: '{wrong}' -> '{correct}' (len {len(wrong)} -> {len(correct)})")
                        continue
                    # 过滤剧本中不存在的"正确词"：若正确词在剧本原文中找不到，说明Kimi在乱猜
                    wrong_chinese = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', wrong)
                    correct_chinese = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', correct)
                    if correct_chinese and correct_chinese not in all_script_text:
                        print(f"   [SKIP] Correct word not in script: '{wrong}' -> '{correct}'")
                        continue
                    # 过滤无关替换：用编辑距离判断错误词和正确词是否差异过大
                    # 例："刘皇叔"→"朱元璋"（编辑距离=3，完全不同的词）
                    if len(wrong_chinese) >= 2 and len(correct_chinese) >= 2:
                        a, b = wrong_chinese, correct_chinese
                        _m, _n = len(a), len(b)
                        _prev = list(range(_n + 1))
                        for _i in range(1, _m + 1):
                            _curr = [_i] + [0] * _n
                            for _j in range(1, _n + 1):
                                _curr[_j] = min(
                                    _prev[_j] + 1,
                                    _curr[_j - 1] + 1,
                                    _prev[_j - 1] + (0 if a[_i - 1] == b[_j - 1] else 1)
                                )
                            _prev = _curr
                        edit_dist = _prev[_n]
                        # 短词（≤6字）只允许1字之差：防止 Kimi 把读音完全不同的词替换（如"有人来了"→"公安来了"）
                        # 长词（>6字）保持 0.75 阈值：允许前缀替换等规则13场景
                        _max_chn_len = max(len(wrong_chinese), len(correct_chinese))
                        threshold = 1 if _max_chn_len <= 6 else _max_chn_len * 0.75
                        if edit_dist > threshold:
                            print(f"   [SKIP] Edit distance too high ({edit_dist} > {threshold:.1f}): '{wrong}' -> '{correct}'")
                            continue
                    replacements[wrong] = correct

        if not replacements:
            print("   No valid replacements parsed")
            return sentences

        print(f"   Applying {len(replacements)} replacements:")
        # 按长度降序排列，优先替换长词（避免短词替换破坏长词）
        sorted_replacements = sorted(replacements.items(), key=lambda x: len(x[0]), reverse=True)

        # 预计算每个 correct 词在剧本哪些行出现（用于位置校验）
        # script_lines 中每个元素有 'line'（纯汉字）字段
        _script_line_texts = [re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', sl.get('line', sl.get('raw', '')))
                               for sl in script_lines]
        _correct_positions: dict = {}
        for _wrong, _correct in sorted_replacements:
            _cc = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', _correct)
            if not _cc:
                continue
            _correct_positions[_correct] = [i for i, lt in enumerate(_script_line_texts) if _cc in lt]

        _total_sents  = len(sentences)
        _total_script = len(_script_line_texts)

        corrected = []
        for _sent_idx, sent in enumerate(sentences):
            text = sent['text']
            for wrong, correct in sorted_replacements:
                if wrong not in text:
                    continue
                # 提前计算当前句子在序列中的相对位置（0~1），供两处位置校验共用
                _expected_pct = (_sent_idx + 0.5) / _total_sents if _total_sents > 0 else 0.5

                # 原文位置保护：若 wrong 本身在剧本中有接近当前位置的匹配，说明 ASR 原文可能正确，不替换
                # （例："我自己能处理" → "那好对了"，"我自己能处理" 在剧本中就在这附近，ASR 应当是对的）
                if _total_script > 0:
                    _wc = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', wrong)
                    if len(_wc) >= 3:
                        _wrong_pos = [i for i, lt in enumerate(_script_line_texts) if _wc in lt]
                        if _wrong_pos:
                            _wrong_gap = min(abs(_expected_pct - p / _total_script) for p in _wrong_pos)
                            if _wrong_gap <= 0.35:
                                print(f"   [SKIP] ASR '{wrong}' found in script at gap={_wrong_gap:.0%}, likely correct")
                                continue

                # 位置校验：correct 对应的剧本行位置与当前句子的序列位置是否吻合
                # 若 correct 只出现在剧本很靠后/靠前的位置，而当前句子还在中间，则拒绝
                _positions = _correct_positions.get(correct, [])
                if _positions and _total_script > 0:
                    _min_gap = min(abs(_expected_pct - p / _total_script) for p in _positions)
                    if _min_gap > 0.35:
                        # 位置偏差过大时跳过替换（遵循"无法确定是剧本哪一段就听ASR的"原则）
                        print(f"   [SKIP] Position mismatch (sent {_sent_idx+1}/{_total_sents}, "
                              f"pos={_expected_pct:.0%}, nearest_script={1-_min_gap:.0%}): "
                              f"'{wrong}' -> '{correct}'")
                        continue
                # 防止双重替换：若 correct 以 wrong 结尾（纯加前缀型，如 "赚钱"→"比赚钱"），
                # 用负向后瞻避免把已含 correct 的句子里的 wrong 再次替换
                # 例："比赚钱".replace("赚钱","比赚钱") → "比比赚钱"（错）
                # 修复：re.sub(r'(?<!比)赚钱', '比赚钱', '比赚钱') → "比赚钱"（正确）
                if correct.endswith(wrong) and len(correct) > len(wrong):
                    _extra = re.escape(correct[:-len(wrong)])
                    new_text = re.sub(r'(?<!' + _extra + r')' + re.escape(wrong), correct, text)
                else:
                    new_text = text.replace(wrong, correct)
                print(f"   '{text}': '{wrong}' -> '{correct}' => '{new_text}'")
                text = new_text
            # 防止过度替换：若替换后汉字数减少超过50%（且原文≥4字），说明Kimi把无关剧本内容强行替入，保留原文
            _orig_clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', sent['text'])
            _new_clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', text)
            if _orig_clean != _new_clean and len(_orig_clean) >= 4 and len(_new_clean) <= len(_orig_clean) // 2:
                print(f"   [REVERT_OVERSUB] '{sent['text']}' -> '{text}'：替换后文字减少超过50%，保留原文")
                text = sent['text']
            corrected.append({**sent, 'text': text})

        print(f"[OK] Text correction done")
        return corrected

    return sentences  # 全部重试失败


def map_segments_to_timestamps(segments, words):
    """
    将 Kimi 输出的文字段落映射回字级时间戳。

    策略：
    - 把所有字拼成一个序列
    - 对每个 segment，在字序列中顺序贪心匹配
    - 找到对应的 words，取第一个字的 start_time 和最后一个字的 end_time
    - 允许纠错：匹配字符数 >= segment长度30% 即视为成功，用已匹配范围确定时间戳
    - 完全匹配失败时，用当前 pos 附近的字作为时间戳锚点（不丢弃句子）
    """
    # 构建字-word索引映射
    char_list = []  # [(char, word_idx), ...]
    for w_idx, word in enumerate(words):
        for ch in word.text:
            char_list.append((ch, w_idx))

    all_text = "".join(c[0] for c in char_list)
    total_chars = len(all_text)

    sentences = []
    pos = 0  # 当前在 all_text 中的位置

    for seg_text in segments:
        if not seg_text.strip():
            # 空行 = 语气词，跳过
            continue

        # 清理 seg_text（只保留汉字和字母数字）
        clean_seg = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf\w]', '', seg_text)
        if not clean_seg:
            continue

        # 贪心匹配：从 pos 开始，顺序匹配 clean_seg 的每个字
        # 搜索窗口放大：最多向前看 len(clean_seg)*5 + 20 个字（容忍大量纠错）
        matched_char_indices = []
        j = pos
        for ch in clean_seg:
            limit = min(j + len(clean_seg) * 5 + 20, total_chars)
            for k in range(j, limit):
                if all_text[k] == ch:
                    matched_char_indices.append(k)
                    j = k + 1
                    break

        match_ratio = len(matched_char_indices) / max(len(clean_seg), 1)

        if not matched_char_indices or match_ratio < 0.15:
            # 匹配率极低（<15%），说明这段文字与原始 ASR 差异极大
            # 用当前 pos 作为锚点，估算时间戳
            anchor = min(pos, total_chars - 1)
            if anchor >= 0 and total_chars > 0:
                anchor_word_idx = char_list[anchor][1]
                start_time = words[anchor_word_idx].start_time
                end_time = start_time + max(len(clean_seg) * 0.2, 0.5)
                print(f"[WARNING] Low match ({match_ratio:.0%}) for '{seg_text}', using anchor timestamp")
                sentences.append({
                    'text': seg_text,
                    'start_time': start_time,
                    'end_time': end_time
                })
            else:
                print(f"[WARNING] Cannot map segment to timestamps: '{seg_text}'")
            continue

        # 取第一个和最后一个匹配字的 word 索引
        start_char_idx = matched_char_indices[0]
        end_char_idx = matched_char_indices[-1]

        start_word_idx = char_list[start_char_idx][1]
        end_word_idx = char_list[end_char_idx][1]

        start_time = words[start_word_idx].start_time
        end_time = words[end_word_idx].end_time

        # 保证最小时长
        if end_time - start_time < 0.3:
            end_time = start_time + 0.3

        # 限制最大时长：每个字约 0.35s，最多不超过 字数*0.35 + 1.5s
        # 避免 ASR 把停顿时间算进 end_time 导致字幕显示时间过长
        max_duration = len(clean_seg) * 0.35 + 1.5
        if end_time - start_time > max_duration:
            end_time = start_time + max_duration

        # 幽灵时间戳检测：若时长已被截断到 max_duration（可能匹配了 ASR 幻觉词），
        # 尝试在 char_list 后续位置重新匹配同一文字串，若找到时长更短的匹配则优先使用
        if abs(end_time - start_time - max_duration) < 0.05:
            _retry_j = end_char_idx + 1
            _retry_matched = []
            for _ch in clean_seg:
                _limit = min(_retry_j + len(clean_seg) * 5 + 20, total_chars)
                for _k in range(_retry_j, _limit):
                    if all_text[_k] == _ch:
                        _retry_matched.append(_k)
                        _retry_j = _k + 1
                        break
            _retry_ratio = len(_retry_matched) / max(len(clean_seg), 1)
            if _retry_ratio >= 0.8 and _retry_matched:
                _r_start_w = char_list[_retry_matched[0]][1]
                _r_end_w   = char_list[_retry_matched[-1]][1]
                _r_start   = words[_r_start_w].start_time
                _r_end     = words[_r_end_w].end_time
                _r_dur     = _r_end - _r_start
                # 若重试匹配时长明显更短（不再触发 max_duration），说明原匹配是幽灵词
                if _r_dur < max_duration * 0.85:
                    print(f"   [GHOST_FIX] '{seg_text}' {start_time:.2f}~{end_time:.2f}s "
                          f"(capped, ghost) -> {_r_start:.2f}~{_r_end:.2f}s (retry, ratio={_retry_ratio:.0%})")
                    start_char_idx = _retry_matched[0]
                    end_char_idx   = _retry_matched[-1]
                    start_word_idx = _r_start_w
                    end_word_idx   = _r_end_w
                    start_time     = _r_start
                    end_time       = _r_end

        sentences.append({
            'text': seg_text,
            'start_time': start_time,
            'end_time': end_time,
            '_end_word_idx': end_word_idx,  # 暂存，用于后续截断
        })

        # 推进位置（用最后一个匹配字的位置）
        pos = end_char_idx + 1

    # 后处理：用下一句的 start_time 截断当前句的 end_time
    # 避免 ASR 把停顿时间算进前一个词的 end_time，导致时间戳过长
    for i in range(len(sentences) - 1):
        curr = sentences[i]
        nxt  = sentences[i + 1]
        if curr['end_time'] > nxt['start_time']:
            # 截断到下一句开始前（留 0.05s 间隔）
            new_end = max(curr['start_time'] + 0.3, nxt['start_time'] - 0.05)
            print(f"   [TRIM] '{curr['text']}' end {curr['end_time']:.2f}s -> {new_end:.2f}s (next starts {nxt['start_time']:.2f}s)")
            curr['end_time'] = new_end

    # 清理临时字段
    for s in sentences:
        s.pop('_end_word_idx', None)

    return sentences


def split_long_duration_sentences(sentences, words, max_duration=5.0, min_gap=2.0):
    """
    对 map_segments_to_timestamps 后持续时间异常长的字幕进行拆分。
    若某条字幕持续超过 max_duration 秒，且其时间范围内存在 >= min_gap 秒的 ASR 停顿，
    则在最大停顿处按字数比例拆分文字和时间戳。

    典型场景：Kimi 断句时把 "来吧[13.5s]让你们见识一下" 合并成一条，
    导致字幕从 73.76s 一直显示到 90.64s（持续 16.88s）。
    """
    result = []
    for sent in sentences:
        duration = sent['end_time'] - sent['start_time']
        if duration <= max_duration:
            result.append(sent)
            continue

        # 找这个时间范围内的 ASR 词语
        s_start, s_end = sent['start_time'], sent['end_time']
        relevant_words = [w for w in words
                          if w.start_time >= s_start - 0.1 and w.end_time <= s_end + 0.1]

        if len(relevant_words) < 2:
            result.append(sent)
            continue

        # 找最大停顿位置
        best_gap = 0.0
        best_split_idx = -1  # 在此 word 之后拆分
        for i in range(len(relevant_words) - 1):
            gap = relevant_words[i + 1].start_time - relevant_words[i].end_time
            if gap > best_gap and gap >= min_gap:
                best_gap = gap
                best_split_idx = i

        if best_split_idx < 0:
            result.append(sent)
            continue

        # 按 relevant_words 的字数计算拆分位置
        chars_before = sum(len(w.text) for w in relevant_words[:best_split_idx + 1])
        text = sent['text']
        text_before = text[:chars_before].strip()
        text_after = text[chars_before:].strip()

        if text_before and text_after:
            print(f"[SPLIT_LONG] '{sent['text']}' (dur={duration:.1f}s, gap={best_gap:.1f}s at "
                  f"{relevant_words[best_split_idx].end_time:.2f}s) -> '{text_before}' | '{text_after}'")
            result.append({**sent, 'text': text_before,
                            'end_time': relevant_words[best_split_idx].end_time})
            result.append({**sent, 'text': text_after,
                            'start_time': relevant_words[best_split_idx + 1].start_time})
        else:
            result.append(sent)

    return result


def salvage_uncovered_words(sentences, words, max_gap_to_neighbor=8.0, min_chars=2):
    """
    检测被 Kimi 断句遗漏的 ASR 词语，将其恢复为独立字幕条目。

    Kimi Step1 断句时可能将 "两界已[1.1s]安我的使命[1.4s]在更高处" 只输出 "安我的使命"，
    导致 "两界已" 和 "在更高处" 对应的 ASR 词未被任何字幕时间范围覆盖，从结果中消失。

    策略：
    - 对每个 ASR 词，检测其中间时间点是否落在某字幕的 [start_time, end_time+0.5s] 内
    - 将所有未覆盖的词按时间分组（组内相邻词间隔 < 0.5s 视为同一句话片段）
    - 若某组包含 >= min_chars 个汉字，且距最近字幕 <= max_gap_to_neighbor 秒，则恢复为新条目
    - 注意：应被多轮调用（第一轮恢复的词可为第二轮创造近邻条件）

    典型场景：
    - 第87集 "两界已安，我的使命...在更高处"，Kimi 只保留中间片段，头尾遗漏
    - 第89集 "姓名/来历/修为" 等短片段，Kimi 整块跳过
    """
    if not sentences or not words:
        return sentences

    def is_covered(word):
        wm = (word.start_time + word.end_time) / 2
        for s in sentences:
            # end_time+0.5s 的宽松检测，容忍 max_duration 截断导致的误判
            if s['start_time'] <= wm <= s['end_time'] + 0.5:
                return True
        return False

    uncovered = [w for w in words if not is_covered(w)]
    if not uncovered:
        return sentences

    # 按时间将未覆盖的词分组（组内间隔 < 0.5s 视为同一句话片段，较大停顿则拆分为独立条目）
    groups = [[uncovered[0]]]
    for i in range(1, len(uncovered)):
        gap = uncovered[i].start_time - uncovered[i - 1].end_time
        if gap < 0.5:
            groups[-1].append(uncovered[i])
        else:
            groups.append([uncovered[i]])

    new_sents = []
    for grp in groups:
        text = ''.join(w.text for w in grp)
        text_clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', text)
        if len(text_clean) < min_chars:
            continue
        g_start = grp[0].start_time
        g_end = grp[-1].end_time
        # 仅在距最近已有字幕 <= max_gap_to_neighbor 秒时才恢复（避免将 ASR 噪声误插入）
        nearest_gap = min(
            min(abs(s['end_time'] - g_start), abs(g_end - s['start_time']))
            for s in sentences
        )
        if nearest_gap > max_gap_to_neighbor:
            print(f"[SALVAGE_SKIP] '{text_clean}' ({g_start:.2f}-{g_end:.2f}s) "
                  f"距最近字幕 {nearest_gap:.1f}s > {max_gap_to_neighbor}s，跳过")
            continue
        print(f"[SALVAGE_UNCOVERED] '{text_clean}' ({g_start:.2f}-{g_end:.2f}s) "
              f"ASR词未被任何字幕覆盖（距最近字幕 {nearest_gap:.1f}s），恢复插入")
        new_sents.append({'text': text_clean, 'start_time': g_start, 'end_time': g_end})

    if not new_sents:
        return sentences

    result = list(sentences) + new_sents
    result.sort(key=lambda s: s['start_time'])
    return result


def attach_short_orphan_to_next(sentences, script_lines):
    """
    将 1-2 字孤立碎片并入下一句（当碎片 + 下一句前缀 = 剧本台词行时）。

    典型场景：
      salvage 恢复 "老师"(13.52s)，下一句是 "我要跳级我要直接考三年级"(18.0s)。
      剧本行 "老师我要跳级"：
        → orphan "老师" + suffix "我要跳级" = "老师我要跳级" ✓
        → "我要跳级我要直接考三年级" 以 "我要跳级" 开头 ✓
        → entry1 = "老师我要跳级"(start=13.52s), entry2 = "我要直接考三年级"(start=18.0s)
    """
    if not script_lines or not sentences:
        return sentences

    script_texts = [
        re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', sl['line'])
        for sl in script_lines
    ]

    result = list(sentences)
    i = 0
    while i < len(result):
        sent = result[i]
        clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', sent['text'])

        # 只处理 1-2 字的孤立碎片
        if len(clean) == 0 or len(clean) > 2:
            i += 1
            continue

        # 后面必须有下一句，且间隔 > 1s（避免处理紧密连接的短词）
        if i + 1 >= len(result):
            i += 1
            continue
        next_sent = result[i + 1]
        gap = next_sent['start_time'] - sent['end_time']
        if gap <= 1.0:
            i += 1
            continue

        next_clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', next_sent['text'])

        # 在剧本中找：以 orphan 开头、且后缀是 next_clean 的前缀的台词行
        matched_line = None
        prefix_in_next = None
        for sline in script_texts:
            if not sline.startswith(clean):
                continue
            suffix = sline[len(clean):]
            if not suffix:
                continue
            if next_clean.startswith(suffix):
                matched_line = sline
                prefix_in_next = suffix
                break

        if matched_line is None:
            i += 1
            continue

        # 计算 next_clean 去掉前缀后的剩余部分
        remainder = next_clean[len(prefix_in_next):]

        # entry1：orphan + 前缀 = 剧本台词行，时间从 orphan.start_time 开始
        est_end1 = sent['start_time'] + len(matched_line) * 0.4
        end1 = min(est_end1, next_sent['start_time'] - 0.05)
        entry1 = {**sent, 'text': matched_line, 'end_time': end1}

        if remainder:
            entry2 = {**next_sent, 'text': remainder}
            print(f"   [ATTACH_ORPHAN] '{sent['text']}'+'{prefix_in_next}' -> '{matched_line}' "
                  f"({sent['start_time']:.2f}s~{end1:.2f}s), "
                  f"remainder='{remainder}' ({next_sent['start_time']:.2f}s~{next_sent['end_time']:.2f}s)")
            result[i] = entry1
            result[i + 1] = entry2
        else:
            print(f"   [ATTACH_ORPHAN] '{sent['text']}'+'{next_clean}' -> '{matched_line}' "
                  f"({sent['start_time']:.2f}s~{end1:.2f}s), next fully consumed")
            result[i] = entry1
            result.pop(i + 1)

        i += 1

    return result


def fallback_merge(words, max_gap=0.4, max_chars=15, min_duration=0.3):
    """备用断句：纯基于时间间隔"""
    if not words:
        return []

    sentences = []
    current = {
        'text': words[0].text,
        'start_time': words[0].start_time,
        'end_time': words[0].end_time
    }

    for i in range(1, len(words)):
        word = words[i]
        prev = words[i - 1]
        gap = word.start_time - prev.end_time

        should_break = (
            gap > max_gap
            or len(current['text']) >= max_chars
            or (prev.end_time - current['start_time']) > 5.0
        )

        if should_break:
            duration = current['end_time'] - current['start_time']
            if duration < min_duration:
                current['end_time'] = current['start_time'] + min_duration
            sentences.append(current)
            current = {
                'text': word.text,
                'start_time': word.start_time,
                'end_time': word.end_time
            }
        else:
            current['text'] += word.text
            current['end_time'] = word.end_time

    if current['text']:
        duration = current['end_time'] - current['start_time']
        if duration < min_duration:
            current['end_time'] = current['start_time'] + min_duration
        sentences.append(current)

    return sentences


def _clip_video_segment(src_path, start_sec, end_sec, out_path):
    """
    用 ffmpeg 从 src_path 截取 [start_sec, end_sec] 片段到 out_path。
    使用 -c copy 保持原画质，速度极快。
    src_path 必须是纯 ASCII 路径（调用方负责）。
    """
    cmd = [
        'ffmpeg', '-y',
        '-ss', str(max(0.0, start_sec)),
        '-to', str(end_sec),
        '-i', str(src_path),
        '-c', 'copy',
        str(out_path)
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg clip failed: {result.stderr.decode('utf-8', errors='replace')[-200:]}"
        )


def kimi_filter_hardcoded_subs(sentences, video_path, api_key, oss_cfg=None, cache_path=None, _attempt=0, script_texts=None):
    """
    让 Kimi 主动扫描整个视频，找出所有有硬字幕的时间段，
    然后过滤掉与这些时间段重叠的 ASR 句子。
    返回需要删除的句子索引集合。

    策略：
    1. 全片原始视频 base64 一次性发给 kimi-k2.5
    2. 让 Kimi 列出视频中所有硬字幕的时间段（start-end 格式）
    3. 对每条 ASR 句子，检查其时间段是否与任何硬字幕时间段重叠
    4. 重叠 → 删除该 ASR 句子，避免叠加显示
    注意：硬字幕可能有错别字，不做文字比对，只看时间段重叠。

    cache_path: 如果指定，将检测结果缓存到该 JSON 文件，下次直接复用。
    """
    import tempfile, shutil

    print("\n[KIMI] Scanning video for hardcoded subtitle segments...")

    video_path = Path(video_path)
    if not video_path.exists():
        print(f"[WARNING] Video not found: {video_path}")
        return set()

    size_mb = video_path.stat().st_size / 1024 / 1024
    print(f"   Video: {video_path.name} ({size_mb:.1f} MB), {len(sentences)} sentences")

    # 尝试从缓存加载（避免重复调用 API）
    if cache_path and Path(cache_path).exists():
        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                cached = json.load(f)
            hard_intervals = [tuple(iv) for iv in cached.get('intervals', [])]
            print(f"   [CACHE] Loaded {len(hard_intervals)} hardcoded intervals from cache")
            # 根据缓存的 intervals 计算 to_remove
            to_remove = set()
            for idx, sent in enumerate(sentences):
                s_start = sent['start_time']
                for h_start, h_end in hard_intervals:
                    if h_start - 0.1 <= s_start <= h_end:
                        # 剧本文字保护：仅当 start_time 落在区间最前 0.5s 内 且 文字 >= 4 字才保护
                        # gap >= 0.5s 说明字幕已在区间内部，是硬字幕内容，不应保护
                        _near_edge = s_start < h_start + 0.5
                        if _near_edge and script_texts:
                            _clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', sent['text'])
                            if len(_clean) >= 4 and _clean in script_texts:
                                print(f"   [{idx+1}] '{sent['text']}' PROTECTED by script_texts (near edge, gap={s_start-h_start:.1f}s), skip removal")
                                break
                        to_remove.add(idx)
                        print(f"   [{idx+1}] '{sent['text']}' start={s_start:.1f}s in hard sub {h_start:.1f}-{h_end:.1f}s")
                        break
            print(f"   Cached: {len(to_remove)} overlapping sentences: {sorted(i+1 for i in to_remove)}")
            return to_remove
        except Exception as e:
            print(f"   [CACHE] Failed to load cache: {e}, will re-detect")

    client = OpenAI(
        api_key=api_key,
        base_url='https://api.moonshot.cn/v1'
    )

    tmp_dir = Path(tempfile.gettempdir())
    tmp_src = tmp_dir / 'kimi_src.mp4'
    shutil.copy2(str(video_path), str(tmp_src))

    try:
        with open(tmp_src, 'rb') as f:
            video_b64 = base64.b64encode(f.read()).decode('utf-8')
        encoded_mb = len(video_b64) / 1024 / 1024
        print(f"   Base64 encoded: {encoded_mb:.1f} MB")

        completion = client.chat.completions.create(
            model='kimi-k2.5',
            messages=[
                {
                    'role': 'system',
                    'content': (
                        '你是视频硬字幕检测助手。\n'
                        '【硬字幕定义】硬字幕是烧录在视频画面上的"字幕条"文字，特征：\n'
                        '  - 固定出现在画面底部（少数在顶部）\n'
                        '  - 白色或黄色文字，带黑色描边或阴影\n'
                        '  - 内容是人物说话的台词\n'
                        '  - 通常持续1-3秒，随台词出现和消失\n'
                        '【不是硬字幕的情况，不要报告】：\n'
                        '  - 画面中央或四周的特效文字（技能名称、招式名称）\n'
                        '  - 场景标题文字（如"三日后·某某地点"）\n'
                        '  - 片头片尾的标题文字\n'
                        '  - 动画特效的发光文字\n'
                        '你的任务是：只找出底部字幕条形式的硬字幕时间段，忽略其他所有文字。'
                    )
                },
                {
                    'role': 'user',
                    'content': [
                        {
                            'type': 'video_url',
                            'video_url': {'url': f'data:video/mp4;base64,{video_b64}'}
                        },
                        {
                            'type': 'text',
                            'text': (
                                '请扫描这个视频，只找出画面底部出现的"字幕条"硬字幕时间段。\n'
                                '注意：技能特效文字、场景标题、片头片尾文字都不算硬字幕，不要报告。\n'
                                '只报告底部字幕条（台词字幕）出现的时间段。\n\n'
                                '输出格式：每行一个时间段，格式为"开始秒数-结束秒数"（如：52.2-53.3）。\n'
                                '如果视频中没有任何底部字幕条，输出"无"。\n'
                                '只输出时间段，不要解释。'
                            )
                        }
                    ]
                }
            ],
            temperature=1.0,
            top_p=0.95,
            timeout=300
        )

        content = completion.choices[0].message.content.strip()
        tokens = completion.usage.prompt_tokens if completion.usage else '?'
        print(f"   Kimi response (tokens={tokens}):\n{content}")

        if content == '无' or not content:
            print("   No hardcoded subs found")
            return set()

        def parse_time_to_seconds(t: str) -> float:
            """支持 '52.3' 纯秒数 或 'HH:MM:SS.s' / 'MM:SS.s' 格式"""
            t = t.strip()
            # HH:MM:SS.s 或 MM:SS.s
            parts = t.split(':')
            if len(parts) >= 2:
                try:
                    nums = [float(p) for p in parts]
                    secs = 0.0
                    for n in nums:
                        secs = secs * 60 + n
                    return secs
                except ValueError:
                    pass
            # 纯秒数
            try:
                return float(t)
            except ValueError:
                return -1.0

        # 解析 Kimi 返回的时间段列表，并前后各扩展 PAD 秒容差
        PAD = 0.5  # Kimi 时间估计可能有偏差，适度扩展容差（不能太大，否则误删相邻台词）
        hard_intervals = []
        for line in content.splitlines():
            line = line.strip()
            # 匹配 "52.2-53.3"、"52-53"、"00:00:09.2-00:00:11.0"、"00:00:09.2 - 00:00:11.0" 等格式
            # 先尝试 HH:MM:SS 格式（含冒号）
            m = re.search(
                r'(\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?)\s*[-–]\s*(\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?)',
                line
            )
            if not m:
                # 再尝试纯秒数格式
                m = re.search(r'(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)', line)
            if m:
                s = parse_time_to_seconds(m.group(1))
                e = parse_time_to_seconds(m.group(2))
                if s >= 0 and e > s:
                    # 扩展容差
                    s_padded = max(0.0, s - PAD)
                    e_padded = e + PAD
                    hard_intervals.append((s_padded, e_padded))
                    print(f"   Hard sub interval: {s:.1f}s - {e:.1f}s  (padded: {s_padded:.1f}s - {e_padded:.1f}s)")

        if not hard_intervals:
            print("   Could not parse any intervals from Kimi response")
            return set()

        # 对每条 ASR 句子，检查其开始时间是否落在硬字幕时间段内
        # 用开始时间判断：句子在硬字幕区间内开始 → 说明这句话是硬字幕台词
        # 不用"重叠"判断，避免跨越边界的长句被误删（如句子在硬字幕前开始但延伸到硬字幕区间）
        to_remove = set()
        for idx, sent in enumerate(sentences):
            s_start = sent['start_time']
            s_end   = sent['end_time']
            for h_start, h_end in hard_intervals:
                # 过滤条件：句子开始时间落在硬字幕区间内（-0.1s 容差，防止浮点误差漏判）
                if h_start - 0.1 <= s_start <= h_end:
                    # 剧本文字保护：仅当 start_time 落在区间最前 0.5s 内 且 文字 >= 4 字才保护
                    # 目的：防止 Kimi 检测偏差导致误把区间起始前的合法字幕拉入区间（偏差通常 < 0.5s）
                    # gap >= 0.5s 说明字幕已在区间内部，是硬字幕内容，不应保护
                    _near_edge = s_start < h_start + 0.5
                    if _near_edge and script_texts:
                        _clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', sent['text'])
                        if len(_clean) >= 4 and _clean in script_texts:
                            print(f"   [{idx+1}] '{sent['text']}' PROTECTED by script_texts (near edge, gap={s_start-h_start:.1f}s), skip removal")
                            break
                    to_remove.add(idx)
                    print(f"   [{idx+1}] '{sent['text']}' start={s_start:.1f}s in hard sub {h_start:.1f}-{h_end:.1f}s")
                    break

        print(f"   Kimi identified {len(to_remove)} overlapping sentences: {sorted(i+1 for i in to_remove)}")

        # 保存检测结果到缓存
        if cache_path:
            try:
                with open(cache_path, 'w', encoding='utf-8') as f:
                    json.dump({'intervals': [list(iv) for iv in hard_intervals]}, f, ensure_ascii=False, indent=2)
                print(f"   [CACHE] Hardcoded intervals saved to {Path(cache_path).name}")
            except Exception as e:
                print(f"   [CACHE] Failed to save cache: {e}")

        return to_remove

    except Exception as e:
        import time as _time
        err_str = str(e)
        if '429' in err_str or 'rate_limit' in err_str.lower() or 'RateLimitError' in type(e).__name__:
            if _attempt < 2:
                wait = 60 * (_attempt + 1)
                print(f"[WARNING] Kimi k2.5 rate limit (429). Waiting {wait}s then retrying ({_attempt+1}/2)...")
                _time.sleep(wait)
                return kimi_filter_hardcoded_subs(
                    sentences, video_path, api_key,
                    oss_cfg=oss_cfg, cache_path=cache_path, _attempt=_attempt + 1
                )
            else:
                print(f"[WARNING] Kimi k2.5 rate limit reached after retries, skipping hardcoded sub detection")
        else:
            print(f"[WARNING] Kimi video analysis error: {e}")
            import traceback
            traceback.print_exc()
        return set()

    finally:
        try:
            tmp_src.unlink()
        except Exception:
            pass


def is_duplicate_of_hardcoded(sent_text, hard_subs, threshold=0.6):
    """
    判断一句字幕是否与硬字幕重复。
    策略：
    1. 子串包含：SRT句子是硬字幕的子串，或硬字幕是SRT句子的子串
    2. 字符序列匹配：SRT句子的字符有 threshold 比例能在硬字幕中按序找到
    """
    clean_sent = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', sent_text)
    if not clean_sent:
        return False

    for hard in hard_subs:
        # 1. 子串包含（最严格的匹配）
        if clean_sent in hard or hard in clean_sent:
            return True

        # 2. 字符序列匹配（允许纠错导致的字符差异）
        # 计算 clean_sent 的字符有多少能在 hard 中按序找到
        j = 0
        matched = 0
        for ch in clean_sent:
            while j < len(hard) and hard[j] != ch:
                j += 1
            if j < len(hard):
                matched += 1
                j += 1
        seq_ratio = matched / max(len(clean_sent), 1)

        # 反向：hard 的字符有多少能在 clean_sent 中按序找到
        j = 0
        matched_rev = 0
        for ch in hard:
            while j < len(clean_sent) and clean_sent[j] != ch:
                j += 1
            if j < len(clean_sent):
                matched_rev += 1
                j += 1
        seq_ratio_rev = matched_rev / max(len(hard), 1)

        if seq_ratio >= threshold and seq_ratio_rev >= threshold:
            return True

    return False


def filter_duplicate_subs(sentences, to_remove_indices):
    """
    根据 Kimi 返回的需要删除的索引集合，过滤句子。
    返回过滤后的句子列表。
    """
    if not to_remove_indices:
        print("\n[DEDUP] No duplicates found")
        return sentences

    filtered = []
    removed = []
    for i, sent in enumerate(sentences):
        if i in to_remove_indices:
            removed.append(sent['text'])
        else:
            filtered.append(sent)

    print(f"\n[DEDUP] Removed {len(removed)} duplicate sentences:")
    for t in removed:
        print(f"   - '{t}'")

    return filtered


def strip_trailing_interjections(sentences):
    """
    去除每句末尾多余的语气词（啊、哦、嗯、呢、吧、哈）。
    仅当末尾字是单个语气词且句子长度 >= 3 时才去除，避免误删短句。
    注意：不处理"好"，因为"好"可能是换人说话（如炎神残魂说"好！"）。
    """
    TRAILING = set('啊哦嗯呢吧哈')
    cleaned = []
    for sent in sentences:
        text = sent['text']
        if len(text) >= 3 and text[-1] in TRAILING:
            new_text = text[:-1]
            print(f"   [STRIP] '{text}' -> '{new_text}' (trailing interjection removed)")
            cleaned.append({**sent, 'text': new_text})
        else:
            cleaned.append(sent)
    return cleaned


def merge_adjacent_single_chars(sentences, script_lines, max_gap_s=1.5):
    """
    将相邻的单字碎片字幕合并成多字词，前提是合并后能在剧本中找到对应内容。
    例：句1="什"(54.0s), 句2="么"(55.2s), gap=1.2s → 合并为"什么"，与剧本"什么"匹配。
    """
    if not script_lines or not sentences:
        return sentences

    # 用 \x00 分隔各行，防止跨行子串误匹配（如"就这"+"什么"→"就这什么"，导致"这什"被误判为有效词）
    all_script_joined = '\x00'.join(
        re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', sl['line'])
        for sl in script_lines
    )

    result = list(sentences)
    i = 0
    while i < len(result):
        clean_i = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', result[i]['text'])
        if len(clean_i) != 1:
            i += 1
            continue
        # 向后收集相邻单字（最多3个，间隔 < max_gap_s）
        group = [i]
        for j in range(i + 1, min(len(result), i + 4)):
            clean_j = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', result[j]['text'])
            if len(clean_j) != 1:
                break
            if result[j]['start_time'] - result[group[-1]]['end_time'] > max_gap_s:
                break
            group.append(j)
        if len(group) < 2:
            i += 1
            continue
        # 从最短子组（2字）开始尝试，找到第一个能在剧本中匹配的子组
        merged = False
        for end_idx in range(2, len(group) + 1):
            sub = group[:end_idx]
            combined = ''.join(
                re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', result[k]['text'])
                for k in sub
            )
            if combined in all_script_joined:
                merged_sent = {**result[i], 'text': combined, 'end_time': result[sub[-1]]['end_time']}
                labels = '+'.join(result[k]['text'] for k in sub)
                print(f"[MERGE_CHARS] '{labels}' -> '{combined}'")
                result[i] = merged_sent
                for k in sorted(sub[1:], reverse=True):
                    result.pop(k)
                merged = True
                break
        i += 1

    return result


def strip_beep_prefix(sentences):
    """
    去除 ASR 将"叮"提示音误识别为"兵"（或"叮"本身）导致的前缀污染。
    典型案例：
      "兵检测到纳灵妖魔" → "检测到纳灵妖魔"
      "兵击杀纳灵级妖魔" → "击杀纳灵级妖魔"
    只在开头字符为"兵"/"叮"/"丁"且紧跟系统通知关键词时才剥除，避免误删正常台词。
    """
    BEEP_CHARS = {'兵', '叮', '丁'}
    SYSTEM_KEYWORDS = ['检测', '击杀', '发现', '警告', '获得', '提升', '吸收', '触发', '解锁', '激活', '任务', '恭喜']
    result = []
    for sent in sentences:
        text = sent['text']
        if text and text[0] in BEEP_CHARS and len(text) > 1:
            rest = text[1:]
            if any(rest.startswith(kw) for kw in SYSTEM_KEYWORDS):
                print(f"   [BEEP_STRIP] '{text}' -> '{rest}' (beep artifact removed)")
                sent = {**sent, 'text': rest}
        result.append(sent)
    return result


def merge_short_sentences(sentences, min_chars=2, min_duration=0.5):
    """
    合并过短的句子
    - 如果一个句子字数 <= min_chars 且时长 < min_duration，尝试与下一句合并
    - 特别处理被错误拆分的情况（如"这" + "是陷害" -> "这是陷害"）
    """
    if len(sentences) <= 1:
        return sentences

    merged = []
    i = 0

    while i < len(sentences):
        curr = sentences[i]

        # 检查当前句子是否过短
        is_short = (len(curr['text'].strip()) <= min_chars and
                   curr['end_time'] - curr['start_time'] < min_duration)

        # 如果过短且不是最后一句，尝试与下一句合并
        if is_short and i < len(sentences) - 1:
            next_sent = sentences[i + 1]

            # 合并文本和时间
            merged_text = curr['text'].strip() + next_sent['text'].strip()
            merged_sent = {
                'text': merged_text,
                'start_time': curr['start_time'],
                'end_time': next_sent['end_time']
            }

            print(f"[MERGE] '{curr['text']}' + '{next_sent['text']}' -> '{merged_text}'")
            merged.append(merged_sent)
            i += 2  # 跳过下一句，因为已经合并了
        else:
            merged.append(curr)
            i += 1

    return merged


def get_video_duration(video_path):
    """使用 ffprobe 获取视频时长"""
    try:
        import subprocess
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', str(video_path)],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            import json
            data = json.loads(result.stdout)
            return float(data['format']['duration'])
    except Exception as e:
        print(f"[WARNING] Failed to get video duration: {e}")
    return None


def extend_last_subtitle(sentences, video_duration, min_duration=0.5):
    """
    延长最后一句字幕的显示时间
    - 如果最后一句字幕的时长 < min_duration，将其延长到视频结束
    - 或者延长到至少 min_duration 秒
    """
    if not sentences:
        return sentences

    last = sentences[-1]
    current_duration = last['end_time'] - last['start_time']

    # 如果最后一句字幕时长过短，延长它
    if current_duration < min_duration:
        # 计算新的结束时间：取视频结束时间和 start_time + min_duration 的较大值
        new_end = max(last['start_time'] + min_duration, video_duration)

        print(f"[EXTEND] Last subtitle '{last['text']}' extended from {last['end_time']:.2f}s to {new_end:.2f}s")
        last['end_time'] = new_end

    return sentences


def filter_interjections(sentences):
    """
    过滤掉纯语气词字幕
    语气词包括：啊、哦、嗯、呢、吧、哈、唉、诶、嘿、喂、哎、呀、哟等
    以及它们的重复形式（啊啊、哦哦等）
    """
    # 常见语气词列表
    interjections = {
        '啊', '哦', '嗯', '呢', '吧', '哈', '唉', '诶', '嘿', '喂',
        '哎', '呀', '哟', '咦', '嘛', '喔', '噢', '唔', '嗨', '咳',
        '嘻', '嘿嘿', '哈哈', '呵呵', '嘻嘻'
    }

    filtered = []
    for sent in sentences:
        text = sent['text'].strip()

        # 检查是否为纯语气词
        is_interjection = False

        # 1. 直接匹配语气词集合
        if text in interjections:
            is_interjection = True

        # 2. 检查是否为单个字符的重复（如：啊啊啊、哦哦）
        if len(set(text)) == 1 and len(text) <= 5:
            char = text[0]
            if char in interjections:
                is_interjection = True

        # 3. 检查是否只包含语气词字符
        if all(c in '啊哦嗯呢吧哈唉诶嘿喂哎呀哟咦嘛喔噢唔嗨咳嘻' for c in text):
            is_interjection = True

        if is_interjection:
            print(f"[FILTER] Removing interjection: {text}")
        else:
            filtered.append(sent)

    return filtered


def fix_overlapping_subtitles(sentences):
    """
    修复字幕时间重叠问题
    - 如果当前字幕的 end_time > 下一字幕的 start_time，则截断当前字幕
    - 如果当前字幕的 start_time >= 下一字幕的 start_time，则调整当前字幕的 end_time
    """
    if len(sentences) <= 1:
        return sentences

    fixed = []
    for i in range(len(sentences)):
        sent = sentences[i].copy()

        # 检查与下一条字幕是否重叠
        if i < len(sentences) - 1:
            next_sent = sentences[i + 1]

            # 如果当前字幕的结束时间晚于下一条的开始时间，截断当前字幕
            if sent['end_time'] > next_sent['start_time']:
                # 留出 0.05 秒的间隙
                sent['end_time'] = max(sent['start_time'] + 0.1, next_sent['start_time'] - 0.05)

            # 如果当前字幕的开始时间等于或晚于下一条的开始时间（异常情况）
            if sent['start_time'] >= next_sent['start_time']:
                # 将当前字幕的结束时间设置为下一条的开始时间之前
                sent['end_time'] = next_sent['start_time'] - 0.05
                # 如果这导致时长过短，则跳过这条字幕
                if sent['end_time'] <= sent['start_time']:
                    print(f"[WARNING] Skipping overlapping subtitle: {sent['text']}")
                    continue

        fixed.append(sent)

    return fixed


def save_srt(sentences, output_path):
    """保存 SRT 文件"""
    with open(output_path, 'w', encoding='utf-8') as f:
        for i, sent in enumerate(sentences, 1):
            f.write(f"{i}\n")
            f.write(f"{format_timestamp(sent['start_time'])} --> {format_timestamp(sent['end_time'])}\n")
            f.write(f"{sent['text']}\n\n")
    print(f"[OK] Subtitles saved: {output_path}")


def burn_subtitles(video_path, srt_path, output_path):
    """
    用 ffmpeg 将 SRT 字幕烧录到视频。
    样式：微软雅黑，细描边，半透明投影。
    """
    print(f"\n[FFMPEG] Burning subtitles into video...")

    # ffmpeg subtitles filter 在 Windows 下路径需要转义冒号
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
        'ffmpeg',
        '-i', str(video_path),
        '-vf', f"subtitles='{srt_str}':force_style='{style}'",
        '-c:a', 'copy',
        str(output_path),
        '-y'
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace'
        )
        if result.returncode == 0:
            print(f"[OK] Preview video saved: {output_path}")
        else:
            print(f"[WARNING] ffmpeg failed (code {result.returncode})")
            # 打印最后几行 stderr
            stderr_lines = result.stderr.strip().split('\n')
            for line in stderr_lines[-5:]:
                print(f"   {line}")
    except FileNotFoundError:
        print("[WARNING] ffmpeg not found, skipping subtitle burn")
    except Exception as e:
        print(f"[WARNING] ffmpeg error: {e}")


def _auto_find_script_dir(video_files, base_dir):
    """
    从视频文件路径自动推断剧名，在 script/ 下模糊匹配对应目录。
    策略：取视频文件名，去掉末尾数字，得到剧名关键词，
    然后在 base_dir/script/ 下找包含该关键词的子目录。
    例：太古灵霄，唯我天尊11.mp4 -> 剧名 "太古灵霄，唯我天尊" -> script/太古灵霄，唯我天尊_Script
    """
    if not video_files:
        return None

    script_root = base_dir / "script"
    if not script_root.exists():
        return None

    # 从第一个视频文件名提取剧名（去掉末尾数字和空格）
    stem = video_files[0].stem
    # 去掉末尾的数字（集数）
    title = re.sub(r'\s*\d+\s*$', '', stem).strip()
    if not title:
        return None

    print(f"[INFO] Auto-detecting script dir for title: '{title}'")

    # 在 script/ 下找包含剧名的子目录（忽略大小写，支持部分匹配）
    best_match = None
    best_score = 0
    for d in script_root.iterdir():
        if not d.is_dir():
            continue
        # 计算剧名与目录名的重叠字符数
        overlap = sum(1 for ch in title if ch in d.name)
        if overlap > best_score:
            best_score = overlap
            best_match = d

    # 至少匹配一半字符才认为有效
    if best_match and best_score >= max(1, len(title) // 2):
        print(f"[OK] Script dir (auto-detected): {best_match}")
        return best_match

    print(f"[WARNING] No matching script dir found for '{title}' in {script_root}")
    return None


def infer_script_dir(video_path):
    """
    根据视频路径自动推断剧本目录
    例如：assets/高手下山：美女请留步/高手下山，美女请留步01/5.mp4
         -> assets/script/高手下山，美女请留步_Script/
    """
    video_path = Path(video_path).absolute()

    # 尝试找到 assets 目录
    for parent in video_path.parents:
        if parent.name == 'assets' or parent.name == 'temp_output':
            # 找到项目根目录
            project_root = parent.parent
            script_base = project_root / 'assets' / 'script'

            if not script_base.exists():
                return None

            # 从视频路径中提取项目名称
            # 例如：assets/高手下山：美女请留步/... -> 高手下山：美女请留步
            # 或：temp_output/5_trimmed_vocals.mp4（需要从原始路径推断）
            if parent.name == 'assets':
                # 视频在 assets 下，取 assets 的下一级目录名
                relative = video_path.relative_to(parent)
                project_name = relative.parts[0] if relative.parts else None
            else:
                # 视频在 temp_output，无法直接推断，返回 None
                return None

            if not project_name:
                return None

            # 在 script 目录下查找匹配的剧本目录
            # 尝试多种命名格式：项目名_Script, 项目名_script, 项目名
            for script_dir in script_base.iterdir():
                if not script_dir.is_dir():
                    continue

                # 移除后缀 _Script 或 _script 进行比较
                dir_name = script_dir.name
                clean_name = re.sub(r'[_\s]*(Script|script)$', '', dir_name, flags=re.IGNORECASE)

                # 比较项目名（忽略标点符号差异）
                clean_project = re.sub(r'[：:，,、]', '', project_name)
                clean_dir = re.sub(r'[：:，,、]', '', clean_name)

                if clean_project == clean_dir or project_name in dir_name:
                    return script_dir

            return None

    return None


def _extract_dialogues_from_txt(txt_path):
    """从关键帧描述 txt 文件中提取 (角色名, 台词) 列表（忽略无台词行）。"""
    STRIP_QUOTES = '"\u201c\u201d\u2018\u2019\u300c\u300d\uff02'
    # 分割点：逗号/分号后紧跟"角色名（...）："或"角色名："格式的位置
    ROLE_SPLIT_RE = re.compile(r'[，,；;]\s*(?=[^（(：:，,；;\s""\u201c\u300c]{1,8}(?:（[^）]*）)?[：:])')

    dialogues = []
    with open(txt_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            m = re.match(r'-?\s*台词[：:](.+)', line)
            if not m:
                continue
            content = m.group(1).strip()
            # 跳过无台词行：如"无"、"无（动作替代台词）"
            if re.match(r'^无[（(]?', content):
                continue

            # 按"角色名（描述）："结构分割多角色行（如 3.txt: 角色C...，角色D...，角色E...）
            segments = ROLE_SPLIT_RE.split(content)

            found = False
            for seg in segments:
                seg = seg.strip()
                role_m = re.match(r'^([^（(：:]{1,8}?)(?:（[^）]*）)?[：:]\s*(.+)$', seg)
                if role_m:
                    speaker = role_m.group(1).strip() or '旁白'
                    dialogue = role_m.group(2).strip().strip(STRIP_QUOTES)
                    if dialogue:
                        dialogues.append((speaker, dialogue))
                        found = True

            if not found:
                # fallback：提取所有引号内容（适用于4.txt纯引号格式），按句子边界拆分
                all_quotes = re.findall(
                    r'["\u201c\u300c\uff02]([^"\u201d\u300d\uff02]+)["\u201d\u300d\uff02]',
                    content
                )
                for q in all_quotes:
                    # 按句子边界（？！。）拆分，避免把多句话合成一个过长的 script line
                    parts = re.split(r'(?<=[？！。])', q)
                    for part in parts:
                        part = part.strip()
                        if part:
                            dialogues.append(('旁白', part))
    return dialogues


def find_script(video_path, script_dir, episode_number=None, txt_folder=None):
    """
    根据视频文件名或指定的集数，找到对应的剧本文件。

    参数：
    - video_path: 视频文件路径
    - script_dir: 剧本目录
    - episode_number: 指定的集数（优先使用，如果为None则从视频文件名提取）
    - txt_folder: 视频原始文件夹，包含 1.txt/2.txt/... 关键帧描述文件（优先级最高）

    规则：
    - 如果提供了 txt_folder，先从该文件夹的所有数字命名 .txt 文件提取台词
    - 否则查找 Episode-XXX.md（3位补零）或 JSON 剧情正文

    例：
    - episode_number=1 -> Episode-001.md 或 Episode-01.md
    - 5_trimmed_vocals.mp4 -> Episode-005.md
    - 太古灵霄，唯我天尊11.mp4 -> Episode-011.md
    """
    # 优先：从原始视频文件夹的 .txt 关键帧描述文件提取台词
    if txt_folder:
        txt_folder_path = Path(txt_folder)
        if txt_folder_path.exists():
            txt_files = sorted(
                [f for f in txt_folder_path.glob('*.txt') if re.match(r'^\d+$', f.stem)],
                key=lambda p: int(p.stem)
            )
            if txt_files:
                all_dialogues = []
                for txt in txt_files:
                    try:
                        all_dialogues.extend(_extract_dialogues_from_txt(txt))
                    except Exception as e:
                        print(f"[WARNING] Failed to read {txt.name}: {e}")
                if all_dialogues:
                    import tempfile as _tempfile
                    # 写成 "角色名：台词" 格式，让 parse_script 能正确解析
                    script_text = '\n'.join(f'{spk}：{dlg}' for spk, dlg in all_dialogues)
                    tmp = _tempfile.NamedTemporaryFile(
                        mode='w', encoding='utf-8', suffix='.md', delete=False
                    )
                    tmp.write(script_text)
                    tmp.close()
                    print(f"[OK] Script loaded from {len(txt_files)} .txt files in {txt_folder_path.name} ({len(all_dialogues)} lines)")
                    return Path(tmp.name)

    if not script_dir:
        return None

    script_dir = Path(script_dir)
    if not script_dir.exists():
        return None

    # 确定集数
    ep_num = episode_number

    if ep_num is None:
        # 从视频文件名提取数字（集数）
        stem = video_path.stem

        # 优先匹配开头的数字（如 5_trimmed_vocals）
        m = re.match(r'^(\d+)', stem)
        if not m:
            # 支持 "第N集" 格式（如 "太古灵霄，唯我天尊_第12集"）
            m = re.search(r'第(\d+)集', stem)
        if not m:
            # 最后尝试匹配末尾的数字
            m = re.search(r'(\d+)\s*$', stem)

        if not m:
            return None

        ep_num = int(m.group(1))

    # 尝试多种 .md 格式
    for fmt in [f"Episode-{ep_num:03d}.md", f"Episode-{ep_num:02d}.md", f"Episode-{ep_num}.md"]:
        script_path = script_dir / fmt
        if script_path.exists():
            return script_path

    # 尝试 JSON 格式：{ep_num:03d}.json，读取 "剧情正文" 字段
    for fmt in [f"{ep_num:03d}.json", f"{ep_num:02d}.json", f"{ep_num}.json"]:
        json_path = script_dir / fmt
        if json_path.exists():
            try:
                import json as _json, tempfile as _tempfile
                with open(json_path, 'r', encoding='utf-8') as f:
                    data = _json.load(f)
                script_text = None
                if isinstance(data, list) and data:
                    script_text = data[0].get('剧情正文', '')
                elif isinstance(data, dict):
                    script_text = data.get('剧情正文', '')
                if script_text:
                    # 将双空格分隔符替换成换行，匹配 parse_script 的 MULTILINE 格式
                    script_text = script_text.replace('  ', '\n')
                    tmp = _tempfile.NamedTemporaryFile(
                        mode='w', encoding='utf-8', suffix='.md', delete=False
                    )
                    tmp.write(script_text)
                    tmp.close()
                    print(f"[OK] Script loaded from JSON: {json_path}")
                    return Path(tmp.name)
            except Exception as e:
                print(f"[WARNING] Failed to load script from JSON {json_path}: {e}")

    return None


def process_video(video_path, asr_model, kimi_api_key, script_dir=None, output_dir=None, episode_number=None, no_burn=False, no_kimi_segment=False, txt_folder=None, text_api_key=None, text_api_url=None, text_api_model=None):
    """
    处理单个视频文件：ASR -> Kimi断句纠错 -> 去重 -> 保存SRT -> 烧录预览。

    参数：
    - episode_number: 指定的集数，用于查找剧本文件
    - no_burn: 如果为True，只生成SRT文件，不烧录字幕到视频
    - no_kimi_segment: 如果为True，禁用Kimi断句，只使用fallback merge
    - txt_folder: 原始视频文件夹路径，含 1.txt/2.txt/... 台词文件（优先作为剧本）

    output_dir: 根输出目录（默认为脚本同级的 output/）
      - 如果指定了output_dir，会在其下创建 subtitle/ 和 video/ 子目录
      - subtitle/ 存放 .srt 和 .json
      - video/ 存放烧录字幕后的 .mp4（文件名与原视频相同）
    """
    import time

    video_path = Path(video_path)

    # 确定输出目录
    if output_dir:
        # 使用指定的输出目录（保持目录结构）
        base_out = Path(output_dir)
        subtitle_dir = base_out
        video_out_dir = base_out / "video"
    else:
        # 默认输出到脚本同级的 output/ 目录
        base_out = Path(__file__).parent / "output"
        subtitle_dir = base_out / "subtitle"
        video_out_dir = base_out / "video"
    
    subtitle_dir.mkdir(parents=True, exist_ok=True)
    video_out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n{'=' * 60}")
    print(f"[VIDEO] {video_path.name}")
    print(f"{'=' * 60}")

    # 找对应剧本
    # 如果没有指定 script_dir，尝试自动推断
    if not script_dir:
        script_dir = infer_script_dir(video_path)
        if script_dir:
            print(f"[OK] Inferred script dir: {script_dir}")

    script_path = find_script(video_path, script_dir, episode_number=episode_number, txt_folder=txt_folder)
    if script_path:
        print(f"[OK] Script: {script_path}")
    else:
        print(f"[WARNING] No script found for {video_path.name}")

    # 执行语音识别
    print(f"\n[PROCESSING] Transcribing...")
    start_time = time.time()

    try:
        results = asr_model.transcribe(
            audio=str(video_path),
            language="Chinese",
            return_time_stamps=True,
        )
        elapsed = time.time() - start_time

        if not results or len(results) == 0:
            print("[ERROR] Transcription failed: No results returned")
            return False

        result = results[0]
        word_timestamps = result.time_stamps

        # 截断异常长时间戳的 ASR 词（如 "去你怎么扔了" end=57.2s），防止污染 map_segments_to_timestamps
        from types import SimpleNamespace as _SNS
        _normalized = []
        for _w in word_timestamps:
            _wc = max(len(re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', _w.text)), 1)
            _w_max_end = _w.start_time + _wc * 1.2 + 1.0
            if _w.end_time > _w_max_end:
                print(f"   [WORD_NORMALIZE] '{_w.text}' end {_w.end_time:.2f}s → {_w_max_end:.2f}s")
                _normalized.append(_SNS(text=_w.text, start_time=_w.start_time, end_time=_w_max_end))
            else:
                _normalized.append(_w)
        word_timestamps = _normalized

        print(f"[OK] Transcription completed in {elapsed:.2f}s")
        print(f"   Words: {len(word_timestamps)}, Text: {result.text[:80]}...")

        # Kimi 第一步：断句（严格使用原始 ASR 文字，不纠错）
        final_sentences = None

        # 文字API配置：优先使用 text_api（如 DeepSeek），其次 kimi_api_key
        _text_key = text_api_key or kimi_api_key
        _text_url = text_api_url or 'https://api.moonshot.cn/v1/chat/completions'
        _text_model = text_api_model or 'moonshot-v1-32k'
        if text_api_key:
            print(f"[INFO] 使用自定义文字API: {_text_url} / {_text_model}")

        if _text_key and not no_kimi_segment:
            segments = kimi_resegment(word_timestamps, _text_key, script_path=script_path,
                                      api_url=_text_url, api_model=_text_model)
            if segments:
                final_sentences = map_segments_to_timestamps(segments, word_timestamps)
                # 对异常长时段字幕（Kimi 合并了大停顿两侧内容）在 ASR 停顿处拆分
                final_sentences = split_long_duration_sentences(final_sentences, word_timestamps)
                # 检测并恢复被 Kimi 断句遗漏的 ASR 词语（多轮迭代：第一轮恢复的词可为第二轮创造近邻条件）
                for _salvage_pass in range(4):
                    _prev_count = len(final_sentences)
                    final_sentences = salvage_uncovered_words(final_sentences, word_timestamps)
                    if len(final_sentences) == _prev_count:
                        break
                print(f"[OK] 断句完成: {len(word_timestamps)} words -> {len(final_sentences)} sentences")
            else:
                print("[WARNING] 断句API返回空结果，使用fallback")
        else:
            if no_kimi_segment:
                print("[INFO] Kimi segmentation disabled (--no-kimi-segment flag)")
            else:
                print("[WARNING] No API key, using fallback merge")

        if not final_sentences:
            final_sentences = fallback_merge(word_timestamps)
            print(f"[OK] Fallback merge: {len(word_timestamps)} words -> {len(final_sentences)} sentences")

        # 保存未经Kimi纠错的原始ASR结果（用于对照分析）
        raw_sentences = [dict(s) for s in final_sentences]  # 深拷贝

        # 将孤立短碎片（1-2字）并入下一句，前提是碎片+下一句前缀 = 剧本台词行
        # 典型场景：salvage 恢复 "老师"(13.52s) + 大间隔 + "我要跳级我要直接考三年级"(18.0s)
        # 剧本行 "老师我要跳级" → 合并为 "老师我要跳级"(13.52s) + "我要直接考三年级"(18.0s)
        if final_sentences and script_path:
            _sls_for_attach = parse_script(script_path)
            if _sls_for_attach:
                final_sentences = attach_short_orphan_to_next(final_sentences, _sls_for_attach)

        # 剥除"叮"提示音被 ASR 误识为"兵"的前缀污染（需在 Kimi 纠错前，避免 Kimi 生成含"兵"的过长替换）
        if final_sentences:
            final_sentences = strip_beep_prefix(final_sentences)

        # 第二步：纠错（对照剧本替换错别字，时间戳不变）
        # 注意：这里只纠正错别字，不替换整句内容
        if _text_key and script_path and final_sentences:
            final_sentences = kimi_correct_sentences(final_sentences, _text_key, script_path,
                                                     api_url=_text_url, api_model=_text_model)
            print("[OK] 纠错完成")

        # 第二步后处理：去除句末多余语气词（啊/哦/嗯/呢/吧/哈）
        if final_sentences:
            final_sentences = strip_trailing_interjections(final_sentences)

        # 对照剧本：修复 ASR 完全失败的多字段落（LCS 匹配分数过低时用剧本尾部补全）
        if final_sentences and script_path:
            _script_lines_for_fix = parse_script(script_path)
            if _script_lines_for_fix:
                final_sentences = merge_adjacent_single_chars(final_sentences, _script_lines_for_fix)
                final_sentences = fix_script_mismatches(final_sentences, _script_lines_for_fix)

        # 过滤孤立单字字幕：
        #   1. 时长极短（≤0.4s）的单字（ASR 噪声）
        #   2. 无法与邻居合并、且不是剧本中独立台词行的单字（如 Kimi 错改后遗留的"陆"）
        if final_sentences:
            _standalone_chars: set = set()
            _all_script_text_for_merge: str = ''  # 剧本所有台词汉字拼接，用于短单字合并检查
            if script_path:
                _sl = parse_script(script_path)
                for sl in _sl:
                    c = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', sl['line'])
                    if len(c) == 1:
                        _standalone_chars.add(c)
                    _all_script_text_for_merge += c

            filtered = []
            for i, sent in enumerate(final_sentences):
                clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', sent['text'])
                dur = sent['end_time'] - sent['start_time']
                if len(clean) == 1:
                    if dur <= 0.4:
                        # 删除前先尝试合并到上一句：若合并后文字在剧本中，则合并而非删除
                        # 例："好我"(0.48s) + "签"(0.32s) → "好我签"（在剧本"好，我签！"中）
                        if filtered and _all_script_text_for_merge:
                            _prev = filtered[-1]
                            _prev_clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', _prev['text'])
                            _merged_clean = _prev_clean + clean
                            if len(_merged_clean) >= 2 and _merged_clean in _all_script_text_for_merge:
                                filtered[-1] = {**_prev, 'text': _prev['text'] + sent['text'], 'end_time': sent['end_time']}
                                print(f"   [MERGE_TO_PREV] '{_prev['text']}'+'{sent['text']}' -> '{filtered[-1]['text']}' (in script)")
                                continue
                        print(f"   [FILTER_NOISE] Removed short single-char '{sent['text']}' ({dur:.2f}s)")
                        continue
                    if clean not in _standalone_chars:
                        prev_clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', final_sentences[i-1]['text']) if i > 0 else ''
                        next_clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', final_sentences[i+1]['text']) if i < len(final_sentences)-1 else ''
                        if len(prev_clean) != 1 and len(next_clean) != 1:
                            print(f"   [FILTER_ISOLATED] Removed isolated single-char '{sent['text']}' (not in script)")
                            continue
                filtered.append(sent)
            final_sentences = filtered

        # 去除连续重复文字的字幕条目（由纠错步骤产生的相邻相同文字，如"废物"+"废物"）
        if final_sentences:
            deduped = [final_sentences[0]]
            for _sent in final_sentences[1:]:
                _clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', _sent['text'])
                _prev_clean = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', deduped[-1]['text'])
                if _clean and _clean == _prev_clean:
                    print(f"   [DEDUP_CONSEC] Removed consecutive duplicate '{_sent['text']}'")
                    continue
                # 检测 Kimi 合并遗留重复：上一句末尾包含当前句全文（允许1字差异）
                # 场景：Kimi 把 "守护苍生"+"可惜此身" 合并为 "守护苍生何惜此身"，但 "可惜此身" 仍残留
                if _clean and len(_clean) >= 3 and len(_prev_clean) > len(_clean):
                    _tail = _prev_clean[-len(_clean):]
                    _diff = sum(a != b for a, b in zip(_tail, _clean))
                    if _diff <= 1:
                        print(f"   [DEDUP_TAIL] '{_sent['text']}' ≈ tail of '{deduped[-1]['text']}' (diff={_diff}), removed")
                        continue
                deduped.append(_sent)
            final_sentences = deduped

        # Kimi 第三步：对照视频硬字幕去重
        if kimi_api_key:
            hardcoded_cache = subtitle_dir / f"{video_path.stem}_hardcoded_intervals.json"
            # 收集剧本中所有台词的汉字集合，用于保护被硬字幕区间误删的正常台词
            _script_texts_for_dedup: set = set()
            if script_path:
                _sl_dedup = parse_script(script_path)
                for _sl in _sl_dedup:
                    _c = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', _sl['line'])
                    if _c:
                        _script_texts_for_dedup.add(_c)
            to_remove = kimi_filter_hardcoded_subs(
                final_sentences, video_path, kimi_api_key, cache_path=hardcoded_cache,
                script_texts=_script_texts_for_dedup if _script_texts_for_dedup else None
            )
            final_sentences = filter_duplicate_subs(final_sentences, to_remove)
            print(f"[OK] After dedup: {len(final_sentences)} sentences")

        # 显示所有句子
        print(f"\n[PREVIEW] Final subtitles ({len(final_sentences)} sentences):")
        for i, sent in enumerate(final_sentences, 1):
            duration = sent['end_time'] - sent['start_time']
            print(f"   {i}. [{sent['start_time']:.2f}s - {sent['end_time']:.2f}s] ({duration:.2f}s) {sent['text']}")

        # 过滤纯语气词字幕
        final_sentences = filter_interjections(final_sentences)
        print(f"[OK] After filtering interjections: {len(final_sentences)} sentences")

        # 延长最后一句字幕（如果太短）
        video_duration = get_video_duration(video_path)
        if video_duration:
            final_sentences = extend_last_subtitle(final_sentences, video_duration)

        # 修复时间重叠问题
        final_sentences = fix_overlapping_subtitles(final_sentences)
        print(f"[OK] Fixed overlapping subtitles: {len(final_sentences)} sentences")

        # 保存原始ASR结果（未经Kimi纠错）→ output/subtitle/
        raw_srt_path = subtitle_dir / f"{video_path.stem}_qwen3_raw.srt"
        save_srt(raw_sentences, raw_srt_path)
        print(f"[OK] Raw SRT saved: {raw_srt_path}")
        
        raw_json_path = subtitle_dir / f"{video_path.stem}_qwen3_raw.json"
        with open(raw_json_path, 'w', encoding='utf-8') as f:
            json.dump({
                'language': result.language,
                'text': result.text,
                'elapsed_time': elapsed,
                'word_count': len(word_timestamps),
                'sentence_count': len(raw_sentences),
                'note': 'This is the raw ASR output before Kimi correction',
                'sentences': [
                    {
                        'text': s['text'],
                        'start_time': s['start_time'],
                        'end_time': s['end_time'],
                        'duration': s['end_time'] - s['start_time']
                    }
                    for s in raw_sentences
                ]
            }, f, ensure_ascii=False, indent=2)
        print(f"[OK] Raw JSON saved: {raw_json_path}")
        
        # 保存 SRT → output/subtitle/
        output_path = subtitle_dir / f"{video_path.stem}_qwen3_optimized.srt"
        # 输出前检查：字均时长过长的字幕可能是 ASR 时间戳锚定到错误位置（如把"啊"的声音误判为多字台词）
        for _chk in final_sentences:
            _chk_chars = len(re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', _chk['text']))
            if _chk_chars >= 3:
                _chk_dur = _chk['end_time'] - _chk['start_time']
                _chk_ratio = _chk_dur / _chk_chars
                if _chk_ratio > 0.8:
                    print(f"[WARNING][时间戳可疑] '{_chk['text']}' "
                          f"({_chk['start_time']:.2f}s-{_chk['end_time']:.2f}s, "
                          f"字均={_chk_ratio:.2f}s/字) — ASR 可能把此处的非语音声音误认为这句台词，"
                          f"请核查视频中 {_chk['start_time']:.1f}s 附近是否有对应的语音")
        save_srt(final_sentences, output_path)

        # 保存 JSON → output/subtitle/
        json_path = subtitle_dir / f"{video_path.stem}_qwen3_optimized.json"
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump({
                'language': result.language,
                'text': result.text,
                'elapsed_time': elapsed,
                'word_count': len(word_timestamps),
                'sentence_count': len(final_sentences),
                'note': 'This is the optimized output after Kimi correction',
                'sentences': [
                    {
                        'text': s['text'],
                        'start_time': s['start_time'],
                        'end_time': s['end_time'],
                        'duration': s['end_time'] - s['start_time']
                    }
                    for s in final_sentences
                ]
            }, f, ensure_ascii=False, indent=2)
        print(f"[OK] JSON saved: {json_path}")

        # 统计
        print(f"\n[STATS] duration={word_timestamps[-1].end_time:.1f}s  "
              f"sentences={len(final_sentences)}  "
              f"avg_len={sum(len(s['text']) for s in final_sentences) / max(len(final_sentences), 1):.1f}chars")

        # ffmpeg 烧录字幕到视频 → output/video/（文件名与原视频相同）
        if not no_burn:
            preview_path = video_out_dir / video_path.name
            burn_subtitles(video_path, output_path, preview_path)
        else:
            print(f"\n[INFO] Skipping subtitle burning (--no-burn flag set)")

        return True

    except Exception as e:
        print(f"\n[ERROR] Processing failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    import argparse
    import time

    parser = argparse.ArgumentParser(
        description='Qwen3-ASR subtitle generation with Kimi correction'
    )
    parser.add_argument(
        '--folder', '-f',
        type=str,
        default=None,
        help='Video folder to process (e.g. assets/太古灵霄，唯我天尊11-20)'
    )
    parser.add_argument(
        '--video', '-v',
        type=str,
        default=None,
        help='Single video file to process'
    )
    parser.add_argument(
        '--script-dir', '-s',
        type=str,
        default=None,
        help='Script directory (e.g. script/太古灵霄，唯我天尊_Script). '
             'Overrides config-example.json script_dir'
    )
    parser.add_argument(
        '--episode', '-e',
        type=int,
        default=None,
        help='Episode number (e.g. 1 for Episode-01.md). '
             'Overrides automatic detection from video filename'
    )
    parser.add_argument(
        '--no-burn',
        action='store_true',
        help='Do not burn subtitles into video, only generate SRT files'
    )
    parser.add_argument(
        '--no-kimi-segment',
        action='store_true',
        help='Disable Kimi segmentation, use fallback merge only'
    )
    parser.add_argument(
        '--config', '-c',
        type=str,
        default=None,
        help='Config file path (default: config-example.json in project root)'
    )
    parser.add_argument(
        '--output-dir', '-o',
        type=str,
        default=None,
        help='Output directory for subtitles and video (default: scripts/output/subtitle/)'
    )
    parser.add_argument(
        '--video-folder',
        type=str,
        default=None,
        help='原始视频文件夹路径（含 1.txt/2.txt/... 台词文件，优先作为剧本）'
    )
    args = parser.parse_args()

    print("=" * 60)
    print("Qwen3-ASR Subtitle Generation (Optimized)")
    print("=" * 60)

    # 加载配置
    if args.config:
        config_path = Path(args.config)
    else:
        config_path = Path(__file__).parent.parent / "config-example.json"
    
    if not config_path.exists():
        print(f"[ERROR] Config file not found: {config_path}")
        return

    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)

    kimi_api_key = config.get('kimi_api_key')
    if not kimi_api_key:
        print("[WARNING] Kimi API key not found in config-example.json")

    # 文字API配置（DeepSeek 或其他 OpenAI 兼容接口）
    deepseek_api_key = config.get('deepseek_api_key')
    text_api_url = config.get('text_api_url')
    text_api_model = config.get('text_api_model')
    if deepseek_api_key:
        text_api_url = text_api_url or 'https://api.deepseek.com/v1/chat/completions'
        text_api_model = text_api_model or 'deepseek-chat'
        print(f"[INFO] DeepSeek API key found, will use for segmentation/correction")

    # 收集要处理的视频文件，同时确定剧本目录
    video_files = []
    script_dir = None  # 先置空，后面推断

    if args.folder:
        folder = Path(__file__).parent / args.folder
        if not folder.exists():
            print(f"[ERROR] Folder not found: {folder}")
            return
        video_files = sorted(
            f for f in folder.glob("*.mp4")
            if not f.stem.endswith('_preview')
        )
        if not video_files:
            print(f"[ERROR] No .mp4 files found in: {folder}")
            return
        print(f"[OK] Found {len(video_files)} video(s) in {folder}")
        for vf in video_files:
            print(f"   - {vf.name}")

    elif args.video:
        vp = Path(__file__).parent / args.video
        if not vp.exists():
            print(f"[ERROR] Video not found: {vp}")
            return
        video_files = [vp]

    else:
        # 默认：处理脚本同目录下的单个测试视频（向后兼容）
        default_video = Path(__file__).parent / "太古灵霄，唯我天尊11.mp4"
        if not default_video.exists():
            print("[ERROR] No video specified and default video not found.")
            print("Usage: python generate_subtitles_qwen3_optimized.py --folder <folder>")
            print("       python generate_subtitles_qwen3_optimized.py --video <video.mp4>")
            return
        video_files = [default_video]

    # 确定剧本目录（优先级：--script-dir > 自动推断 > config-example.json script_dir）
    if args.script_dir:
        script_dir = Path(__file__).parent / args.script_dir
        if not script_dir.exists():
            print(f"[WARNING] Script dir not found: {script_dir}")
            script_dir = None
        else:
            print(f"[OK] Script dir (from --script-dir): {script_dir}")
    else:
        # 从视频路径自动推断剧名，在 script/ 下模糊匹配
        script_dir = _auto_find_script_dir(video_files, Path(__file__).parent)
        if not script_dir:
            # 回退到 config-example.json 里的 script_dir
            cfg_script_dir = config.get('script_dir')
            if cfg_script_dir:
                script_dir = Path(__file__).parent.parent / cfg_script_dir
                if not script_dir.exists():
                    script_dir = None
                else:
                    print(f"[OK] Script dir (from config-example.json): {script_dir}")

    # 检测设备
    if torch.cuda.is_available():
        device = "cuda:0"
        print(f"\n[OK] Using GPU: {torch.cuda.get_device_name(0)}")
    else:
        device = "cpu"
        print("\n[WARNING] Using CPU (slower)")

    # 加载模型（只加载一次，批量复用）
    print("\n[LOADING] Loading Qwen3-ASR model...")
    try:
        asr_model = Qwen3ASRModel.from_pretrained(
            str(QWEN3_ASR_PATH / "models" / "Qwen3-ASR-1.7B"),
            dtype=torch.bfloat16,
            device_map=device,
            max_inference_batch_size=32,
            max_new_tokens=4096,
            forced_aligner=str(QWEN3_ASR_PATH / "models" / "Qwen3-ForcedAligner-0.6B"),
            forced_aligner_kwargs={
                "dtype": torch.bfloat16,
                "device_map": device,
            }
        )
        print("[OK] Model loaded successfully")
    except Exception as e:
        print(f"[ERROR] Model loading failed: {e}")
        return

    # 批量处理
    total = len(video_files)
    success = 0
    t_start = time.time()

    for idx, video_path in enumerate(video_files, 1):
        print(f"\n[{idx}/{total}] Processing: {video_path.name}")
        ok = process_video(
            video_path,
            asr_model,
            kimi_api_key,
            script_dir=script_dir,
            output_dir=args.output_dir,
            episode_number=args.episode,
            no_burn=args.no_burn,
            no_kimi_segment=args.no_kimi_segment,
            txt_folder=args.video_folder,
            text_api_key=deepseek_api_key,
            text_api_url=text_api_url,
            text_api_model=text_api_model,
        )
        if ok:
            success += 1

    elapsed_total = time.time() - t_start
    print(f"\n{'=' * 60}")
    print(f"[DONE] {success}/{total} videos processed in {elapsed_total:.1f}s")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
