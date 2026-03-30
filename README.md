# 视频剪辑自动化流程

完整的视频剪辑自动化工具，整合了黑屏检测、声道分离、转场拼接、字幕生成四个步骤。

```
原始视频片段 (1.mp4, 2.mp4, ...)
        │
        ▼ 步骤1: 黑屏检测 (001remove_black.py)
  去除开头/结尾黑屏帧
        │
        ▼ 步骤2: 声道分离 (002separate.py)
  AI 提取人声，去除背景音乐
        │
        ▼ 步骤3: 转场拼接 (004transition_simple.py)
  合并所有片段 + 混入 BGM
        │
        ▼ 步骤4: 字幕生成与烧录 (003subtitles.py)
  Qwen3-ASR 识别 → Kimi 纠错 → 烧录字幕
        │
        ▼
  output/003output/项目名/集数.mp4  ✅
```

## 项目结构

```
video-editing/
├── run_all.py                    # 主运行脚本
├── config-example.json           # 配置示例（复制为 config.json 后填入 API Key）
├── requirements.txt              # Python 依赖列表
├── README.md
├── .gitignore
│
├── scripts/                      # 主流程脚本
│   ├── 001remove_black.py        # 步骤1: 黑屏检测与删除
│   ├── 002separate.py            # 步骤2: 声道分离（人声提取）
│   ├── 004transition_simple.py   # 步骤3: 转场拼接
│   └── 003subtitles.py           # 步骤4: 字幕生成与烧录
│
├── tools/
│   └── qwen3-asr-deployment/     # Qwen3-ASR 推理服务部署包（独立运行）
│
├── libs/                         # 依赖库（需手动克隆，见下方说明）
│   ├── Music-Source-Separation-Training-GUI/   # 步骤2 声道分离
│   └── Qwen3-ASR/                              # 步骤4 语音识别
│
├── assets/                       # 素材目录（不提交到仓库）
├── temp_output/                  # 临时处理文件（不提交到仓库）
└── output/                       # 最终输出（不提交到仓库）
```

## 工作流程

**处理顺序：步骤1 黑屏检测 → 步骤2 声道分离 → 步骤3 转场拼接 → 步骤4 字幕生成与烧录**

1. **步骤1: 黑屏检测与删除** (`scripts/001remove_black.py`)
   - 输入: 原始视频片段
   - 处理: 检测并删除视频开头/结尾的黑屏帧（含淡入淡出）
   - 输出: `temp_output/.../1_trimmed.mp4`

2. **步骤2: 声道分离** (`scripts/002separate.py`)
   - 输入: 步骤1的输出
   - 处理: AI 模型分离人声+音效，去除背景音乐
   - 输出: `temp_output/.../1_trimmed_vocals.mp4`
   - 依赖: conda 环境 `msst`（见下方安装说明）

3. **步骤3: 转场拼接** (`scripts/004transition_simple.py`)
   - 输入: 步骤2的所有输出视频
   - 处理: 将多个视频片段拼接，可选添加转场效果和背景音乐
   - 输出: `output/004output/项目名/集数文件夹.mp4`

4. **步骤4: 字幕生成与烧录** (`scripts/003subtitles.py`)
   - 输入: 步骤3的拼接视频
   - 处理: Qwen3-ASR 语音识别 → Kimi API 断句纠错 → 对照剧本优化 → 烧录字幕
   - 输出:
     - 字幕文件: `scripts/output/subtitle/项目名/集数文件夹/xxx_qwen3_optimized.srt`
     - 最终视频: `output/003output/项目名/集数文件夹.mp4`
   - 依赖: `libs/Qwen3-ASR/`、Kimi API、`tools/qwen3-asr-deployment/`

## 环境准备

### 1. Python 依赖

```bash
pip install -r requirements.txt
```

### 2. FFmpeg

```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt install ffmpeg

# 验证安装
ffmpeg -version
```

### 3. Conda 环境（步骤2 声道分离专用）

```bash
conda create -n msst python=3.10
conda activate msst
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
pip install librosa soundfile
```

> 声道分离使用 GPU 加速，建议在有 CUDA 的机器上运行。CPU 模式极慢。

### 4. 依赖库（libs/）

```bash
# 步骤2 声道分离依赖
git clone https://github.com/ZFTurbo/Music-Source-Separation-Training libs/Music-Source-Separation-Training-GUI

# 步骤4 语音识别依赖
git clone https://github.com/QwenLM/Qwen3-ASR libs/Qwen3-ASR
```

模型权重文件（`.ckpt`/`.pth`）不包含在仓库中，需自行下载并放入 `libs/Music-Source-Separation-Training-GUI/` 对应目录。

### 5. Qwen3-ASR 模型

下载模型文件后放入 `libs/Qwen3-ASR/models/`，然后参考 `tools/qwen3-asr-deployment/` 中的部署说明启动 ASR 服务。

## 配置文件

复制示例配置并填入你的 API Key：

```bash
cp config-example.json config.json
```

`config.json` 已加入 `.gitignore`，不会提交到仓库。

主要配置项：

```json
{
  "kimi_api_key": "你的 Kimi API Key",
  "deepseek_api_key": "你的 DeepSeek API Key（可选）",
  "msst_conda_env": "msst",
  "remove_black": {
    "brightness_threshold": 50,
    "fade_threshold": 110,
    "use_ai": false
  },
  "transition": {
    "use_random_transitions": true,
    "bgm_volume": 0.3
  }
}
```

## 素材文件夹结构

```
assets/
└── 项目名/
    ├── 集数文件夹/              # 文件夹名包含集数编号即可，格式灵活
    │   ├── 1.mp4               # 视频片段（必须按数字命名）
    │   ├── 2.mp4
    │   ├── 3.mp4
    │   ├── bgm.flac            # 背景音乐（可选，支持 flac/mp3/wav/m4a）
    │   └── 封面.jpg            # 封面图（可选，jpg/png）
    └── 项目名-设定集/           # 剧本目录（可选，用于字幕纠错）
        └── Episode-01.md
```

**集数文件夹命名规则：**
- 脚本从文件夹名中自动提取集数编号
- 支持：`第01集`、`情深-01`、`朱标-03` 等含数字的命名

**视频文件命名规则：**
- 必须按数字命名：`1.mp4`, `2.mp4`, `3.mp4`, ...
- 脚本按数字顺序自动拼接

## 使用方法

### 处理单集

```bash
python run_all.py --folder assets/项目名/集数文件夹
```

### 批量处理整个项目

```bash
python run_all.py --folder assets/项目名
```

批量模式下会先检查每一集是否具备「三要素」（背景音乐、封面、剧本），缺失则报错停止。

### 跳过已完成的步骤

```bash
# 跳过步骤1和2（黑屏检测和声道分离已做过）
python run_all.py --folder assets/项目名/集数文件夹 --skip 1 2

# 只执行字幕生成与烧录
python run_all.py --folder assets/项目名/集数文件夹 --skip 1 2 3
```

步骤编号：`1`=黑屏检测 `2`=声道分离 `3`=转场拼接 `4`=字幕生成

### 使用自定义配置文件

```bash
python run_all.py --folder assets/项目名/集数文件夹 --config my_config.json
```

> 不传 `--config` 时，自动优先读取 `config.json`，找不到则 fallback 到 `config-example.json`（字幕步骤会因 API Key 无效而失败）。

## 输出目录结构

```
output/
├── 004output/项目名/集数文件夹.mp4     # 转场拼接后（未烧录字幕）
└── 003output/项目名/集数文件夹.mp4     # 字幕烧录后的最终视频

scripts/output/subtitle/项目名/集数文件夹/
├── xxx_qwen3_optimized.srt             # 字幕文件
├── xxx_qwen3_optimized.json            # 字幕元数据
└── video/集数文件夹.mp4                # 带字幕视频（003output 的来源）

temp_output/项目名/集数文件夹/
├── 1_trimmed.mp4                       # 黑屏裁剪后
├── 1_trimmed.wav                       # 分离用音频
└── 1_trimmed_vocals.mp4                # 人声替换后
```

所有输出目录都保持与 `assets/` 相同的层级结构。

## 单独运行各步骤

```bash
# 步骤1: 黑屏检测
python scripts/001remove_black.py video.mp4 -o temp_output/

# 步骤2: 声道分离（需在 msst 环境下）
conda run -n msst python scripts/002separate.py video.mp4 temp_output/

# 步骤3: 转场拼接
python scripts/004transition_simple.py --folder assets/项目名/集数文件夹 -o output.mp4 --bgm bgm.flac

# 步骤4: 字幕生成与烧录
python scripts/003subtitles.py --video output.mp4 --config config.json
```

## 常见问题

### 声道分离失败

```bash
conda activate msst
python -c "import torch; print(torch.cuda.is_available())"
```

确认 PyTorch 能识别 GPU。如果返回 `False`，需重新安装 CUDA 版本的 PyTorch。

### 字幕生成失败

- 检查 Qwen3-ASR 服务是否已启动（`tools/qwen3-asr-deployment/`）
- 检查 `config.json` 中 `kimi_api_key` 是否有效
- 检查模型文件是否在 `libs/Qwen3-ASR/models/`

### API Key 无效

确认使用的是 `config.json` 而不是 `config-example.json`：
```bash
python run_all.py --folder assets/项目名/集数文件夹 --config config.json
```

### 内存/显存不足

- 步骤2（声道分离）对显存要求较高，建议 8GB+ 显存
- 可以先 `--skip 2` 跳过声道分离测试其他步骤

## 性能建议

- GPU 加速：步骤2 和步骤4 均需 CUDA，无 GPU 时非常慢
- 批量处理：批量模式会复用已加载的模型，比逐集运行效率更高
- 跳过步骤：已处理过的步骤用 `--skip` 跳过，避免重复计算

## 许可证

本项目整合了多个开源工具，请遵守各工具的许可证：
- [Music-Source-Separation-Training](https://github.com/ZFTurbo/Music-Source-Separation-Training)
- [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR)
- [ffmpeg-python](https://github.com/kkroening/ffmpeg-python)

## 贡献

欢迎提交 Issue 和 Pull Request。
