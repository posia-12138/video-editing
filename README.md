# 视频剪辑自动化流程

短剧混剪全流程自动化工具，覆盖从原始素材到剪映草稿的完整链路：黑屏检测、人声分离、转场拼接、ASR 字幕、AI 选曲，最终生成可直接在剪映中打开的草稿并同步到百度网盘。

## 项目结构

```
剪辑/
├── run_all_v2.py                 # 🎯 主运行脚本（步骤 1-5 全流程）
├── config.json                   # ⚙️ 配置文件（从 config-example.json 复制）
├── config-example.json           # ⚙️ 配置模板
│
├── scripts/                      # 📜 核心脚本
│   ├── 001remove_black.py        # 步骤1: 黑屏检测与裁剪
│   ├── 002separate.py            # 步骤2: 人声分离（msst conda 环境）
│   ├── 004transition_v2.py       # 步骤3: 转场拼接 + 场景检测
│   ├── 003subtitles_simple.py    # 步骤4: ASR 字幕生成（Qwen3）
│   └── select_music.py           # 步骤5: AI 选曲 + 剪映草稿生成
│
├── skills/                       # 🧠 AI 提示词
│   ├── music_director.md         # Kimi 选曲提示词（含情感分析）
│   └── music_tagger.md           # 音乐库打标提示词
│
├── assets/                       # 🎬 素材（不入库）
│   └── [剧名]/
│       ├── [集数]/               # 视频片段文件夹
│       │   ├── 1.mp4 … N.mp4
│       │   └── 封面.jpg
│       └── [剧名]-设定集/        # 剧本文件夹
│           ├── Episode-01.md
│           └── Episode-02.md
│
├── 音乐/                         # 🎵 音乐库（不入库）
│   └── [风格分类]/
│       ├── completed/            # 已打标的音乐
│       │   └── music_tags.json
│       └── [音乐文件.mp3]
│
├── libs/                         # 📚 依赖库（子模块/不入库）
│   └── jianying-editor-skill/    # 剪映草稿生成库
│
├── qwen3-asr-deployment/         # 🎤 ASR 模型部署（子模块）
├── temp_output/                  # 🔄 临时文件（不入库）
└── output/                       # ✅ 输出视频（不入库）
```

## 工作流程

```
原始片段 (1.mp4 … N.mp4)
    │
    ▼ 步骤1: 001remove_black.py
黑屏裁剪后的片段
    │
    ▼ 步骤2: 002separate.py（conda msst）
人声分离后的片段
    │
    ▼ 步骤3: 004transition_v2.py
拼接视频 (004output/xxx.mp4) + 片段清单 (_clips.json)
    │
    ▼ 步骤4: 003subtitles_simple.py（Qwen3-ASR + Kimi 纠错）
字幕文件 (_qwen3_optimized.srt) + 烧录视频 (003output/)
    │
    ▼ 步骤5: select_music.py（Kimi 选曲）
剪映草稿（多段 BGM + 字幕轨 + 独立片段轨）→ 同步百度网盘
```

### 步骤详解

**步骤1：黑屏检测** (`scripts/001remove_black.py`)
- 检测并裁剪片段开头/结尾的纯黑帧（保留有文字的黑屏）
- 支持淡入淡出检测

**步骤2：人声分离** (`scripts/002separate.py`)
- 使用 MSST 模型从原声轨中去除背景音乐，保留人声+音效
- 依赖 conda 环境 `msst`

**步骤3：转场拼接** (`scripts/004transition_v2.py`)
- 将处理后的片段按数字顺序拼接
- 对照剧本（Episode-XX.md）预测场景切换位置
- 在真正的场景切换处加淡入淡出 + 黑场（使用边界帧颜色距离 ≥ 70 判断）
- 生成 `_clips.json` 片段清单供后续步骤使用
- 可单独使用：`python scripts/004transition_v2.py --folder 片段目录 -o 输出.mp4`

**步骤4：字幕生成** (`scripts/003subtitles_simple.py`)
- Qwen3-ASR 语音识别
- Kimi API 对照剧本纠错
- 输出 SRT 字幕 + 烧录视频

**步骤5：AI 选曲 + 剪映草稿** (`scripts/select_music.py`)
- 读取 1000+ 曲目的已标注音乐库
- 调用 Kimi API，结合剧本情感分析和色彩信息，选出多段 BGM
- 生成剪映草稿：独立片段轨 + 多段 BGM 交叉淡化 + 字幕轨
- 自动同步到百度网盘
- 可单独使用：`python scripts/select_music.py --folder assets/剧名/集数`

## 环境准备

### Python 依赖

```bash
pip install requests librosa soundfile numpy
```

### FFmpeg

```bash
ffmpeg -version  # 确保已安装
```

### Conda 环境（步骤2 人声分离）

```bash
conda create -n msst python=3.10
conda activate msst
pip install torch librosa soundfile
# 安装 MSST 模型，参考 libs/Music-Source-Separation-Training-GUI
```

### Qwen3-ASR（步骤4 字幕生成）

参考 `qwen3-asr-deployment/` 子模块中的说明部署本地 ASR 服务。

### Kimi API（步骤4 纠错 / 步骤5 选曲）

在 [Moonshot 平台](https://platform.moonshot.cn/) 获取 API Key，填入 `config.json`。

### 剪映草稿库

`libs/jianying-editor-skill/` 子模块，用于生成剪映草稿文件。

## 配置

```bash
cp config-example.json config.json
# 编辑 config.json，填入 kimi_api_key 和百度网盘路径
```

关键配置项：

| 字段 | 说明 |
|------|------|
| `kimi_api_key` | Kimi API 密钥 |
| `msst_conda_env` | 人声分离 conda 环境名 |
| `draft_package.windows_sync_path` | 百度网盘 Windows 本地路径 |
| `draft_package.mac_sync_path` | 百度网盘 Mac 本地路径 |
| `draft_package.draft_subfolder` | 剪映草稿子目录名 |

## 使用方法

### 完整流程（单集）

```bash
python run_all_v2.py --folder assets/剧名/集数
```

### 批量处理（整剧）

```bash
python run_all_v2.py --folder assets/剧名
# 自动检测子文件夹，逐集处理
```

### 跳过已完成的步骤

```bash
# 只跑转场和选曲（跳过黑屏/分离/字幕）
python run_all_v2.py --folder assets/剧名/集数 --skip 1 2 4

# 只重新选曲生成草稿
python run_all_v2.py --folder assets/剧名/集数 --skip 1 2 3 4

# 只测试转场检测
python run_all_v2.py --folder assets/剧名/集数 --skip 1 2 4 5
```

步骤编号：`1`=黑屏 `2`=分离 `3`=转场 `4`=字幕 `5`=剪映草稿

### 素材目录结构

```
assets/
└── 剧名/
    ├── 第01集/
    │   ├── 1.mp4 … N.mp4    # 片段按数字命名
    │   └── 封面.jpg
    └── 剧名-设定集/
        ├── Episode-01.md    # 剧本（用于场景检测和字幕纠错）
        └── Episode-02.md
```

剧本格式（Episode-XX.md）：
```markdown
## 场景 1-1：海边
...台词/场景描述...

## 场景 1-2：大殿
...台词/场景描述...
```

### 音乐库管理

```bash
# 给新音乐打标签（调用 Kimi 分析）
python scripts/tag_music.py --folder 音乐/风格分类/专辑名

# 打完标签的音乐自动移入 completed/ 子目录
```

`music_tags.json` 格式示例：
```json
{
  "曲目名.mp3": {
    "genre": "国风史诗",
    "mood": ["悲壮", "激昂"],
    "energy": "high",
    "scene": ["战斗", "复仇"]
  }
}
```

## 输出结构

```
output/
├── 004output/剧名/集数.mp4       # 转场拼接视频（无字幕）
└── 003output/剧名/集数.mp4       # 字幕烧录视频

scripts/output/subtitle/剧名/集数/
├── 集数_qwen3_optimized.srt
├── 集数_qwen3_optimized.json
└── video/集数.mp4

百度网盘/JianyingPro Drafts/集数/  # 剪映草稿（自动同步）
```

## 许可证

本项目整合了多个开源工具，请遵守各工具的许可证。
