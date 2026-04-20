# -*- coding: utf-8 -*-
"""
003subtitles_simple.py — 003subtitles.py 的简化版本，供 run_all_v3.py 使用。

与完整版的唯一区别：跳过 kimi_filter_hardcoded_subs（kimi-k2.5 视频扫描）。
Kimi 断句 + 纠错（moonshot-v1-32k 纯文字）仍然保留。

用法与 003subtitles.py 完全相同，参数接口一致。
"""

import sys
from pathlib import Path

# 把 003subtitles.py 所在目录加入 sys.path，确保能 import
_here = Path(__file__).parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

import importlib.util as _ilu

# 动态加载 003subtitles（文件名以数字开头，不能直接 import）
_spec = _ilu.spec_from_file_location("subtitles_full", _here / "003subtitles.py")
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

# ── Monkey-patch：让硬字幕扫描函数直接返回空结果 ──────────────────────────────
def _skip_hardcoded_scan(sentences, video_path, api_key, *args, **kwargs):
    """简化版：跳过 kimi-k2.5 视频扫描，假设没有硬字幕。"""
    print("\n[SKIP] kimi_filter_hardcoded_subs disabled in simple mode")
    return []   # 返回空列表 = 没有需要删除的字幕

_mod.kimi_filter_hardcoded_subs = _skip_hardcoded_scan

# ── 直接调用原始 main() ───────────────────────────────────────────────────────
if __name__ == "__main__":
    _mod.main()
