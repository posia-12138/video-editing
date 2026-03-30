# 视频剪辑自动化流程

针对短剧素材的全流程自动化剪辑工具，串联黑屏检测、声道分离、转场拼接、字幕生成四个步骤，通过 `run_all.py` 一键运行。

```
原始视频片段 (1.mp4, 2.mp4, ...)
        │
        ▼ 步骤1  黑屏检测 (001remove_black.py)
   去除开头/结尾黑屏帧及淡入淡出
        │
        ▼ 步骤2  声道分离 (002separate.py)
   AI 提取人声，去除原片背景音乐
        │
        ▼ 步骤3  转场拼接 (003transition_simple.py)
   合并所有片段 + 混入 BGM
        │
        ▼ 步骤4  字幕生成与烧录 (004subtitles.py)
   Qwen3-ASR 识别 → Kimi 断句纠错 → 烧录字幕
        │
        ▼
  output/final/项目名/集数.mp4  ✅
```

## 效果预览

以「重生成为超级财阀」第01集为例：

| | 路径 |
|---|---|
| 输入素材 | `assets/重生成为超级财阀/财阀-01/` |
| 最终输出 | [`output/final/重生成为超级财阀/财阀-01.mp4`](output/final/重生成为超级财阀/财阀-01.mp4) |

## 项目结构

```
video-editing/
├── run_all.py                    # 主运行脚本
├── config-example.json           # 配置示例（复制为 config.json 后填入 API Key）
├── requirements.txt              # Python 依赖
├── README.md
├── .gitignore
│
├── scripts/
│   ├── 001remove_black.py        # 步骤1: 黑屏检测与删除
│   ├── 002separate.py            # 步骤2: 声道分离
│   ├── 003transition_simple.py   # 步骤3: 转场拼接
│   └── 004subtitles.py           # 步骤4: 字幕生成与烧录
│
├── tools/
│   └── qwen3-asr-deployment/     # Qwen3-ASR 推理服务部署包
│
└── libs/                         # 依赖库（需手动克隆，见下方说明）
    ├── Music-Source-Separation-Training-GUI/   # 步骤2 声道分离
    └── Qwen3-ASR/                              # 步骤4 语音识别
```

> `temp_output/`、`output/transition/`、`scripts/output/` 均已加入 `.gitignore`，不提交到仓库。`assets/` 和 `output/final/` 下的示例项目文件除外。

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
```

### 3. Conda 环境（步骤2 声道分离专用）

步骤2 使用独立 conda 环境运行，避免与主环境的依赖冲突：

```bash
conda create -n msst python=3.10
conda activate msst
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
pip install librosa soundfile
```

> 声道分离强依赖 GPU，CPU 模式处理速度极慢（建议 8GB+ 显存）。

### 4. 依赖库（libs/）

```bash
# 步骤2 声道分离
git clone https://github.com/ZFTurbo/Music-Source-Separation-Training libs/Music-Source-Separation-Training-GUI

# 步骤4 语音识别
git clone https://github.com/QwenLM/Qwen3-ASR libs/Qwen3-ASR
```

下载声道分离模型权重（`.ckpt`）后放入 `libs/Music-Source-Separation-Training-GUI/pretrain/`。

### 5. Qwen3-ASR 模型（步骤4）

下载模型文件（约 4.6GB）后放入 `libs/Qwen3-ASR/models/Qwen3-ASR-1.7B/`：

- Hugging Face：https://huggingface.co/Qwen/Qwen3-ASR-1.7B

参考 `tools/qwen3-asr-deployment/` 中的说明启动推理服务，再运行步骤4。

## 配置文件

```bash
cp config-example.json config.json
```

`config.json` 已加入 `.gitignore`，填入 API Key 后不会提交到仓库。

主要配置项（详见 `config-example.json` 中的注释）：

| 字段 | 必填 | 说明 |
|------|------|------|
| `kimi_api_key` | 是 | 步骤4 字幕断句纠错，申请地址：https://platform.moonshot.cn/ |
| `deepseek_api_key` | 否 | 当前版本暂未启用，留空即可 |
| `msst_conda_env` | 是 | 步骤2 使用的 conda 环境名，默认 `msst` |

## 素材文件夹结构

```
assets/
└── 项目名/
    ├── 集数文件夹/              # 文件夹名中需含集数编号（见下方说明）
    │   ├── 1.mp4               # 视频片段，必须按数字命名
    │   ├── 2.mp4
    │   ├── bgm.flac            # 背景音乐（可选，支持 flac/mp3/wav/m4a）
    │   └── 封面.jpg            # 封面图（可选，支持 jpg/png）
    └── 项目名-设定集/           # 剧本目录（可选，用于步骤4字幕纠错）
        └── Episode-01.md
```

**集数文件夹命名**：脚本自动从文件夹名提取集数编号，支持 `第01集`、`情深-01`、`朱标-03` 等格式。

## 使用方法

### 处理单集

```bash
python run_all.py --folder assets/项目名/集数文件夹
```

### 批量处理整个项目

```bash
python run_all.py --folder assets/项目名
```

批量模式下会先检查每一集的「三要素」（背景音乐、封面、剧本），有缺失则报错停止。

### 跳过已完成的步骤

```bash
# 跳过步骤1、2，从转场拼接开始
python run_all.py --folder assets/项目名/集数文件夹 --skip 1 2

# 只跑字幕生成
python run_all.py --folder assets/项目名/集数文件夹 --skip 1 2 3
```

步骤编号：`1` 黑屏检测　`2` 声道分离　`3` 转场拼接　`4` 字幕生成

### 指定配置文件

```bash
python run_all.py --folder assets/项目名/集数文件夹 --config config.json
```

> 不传 `--config` 时自动优先读取 `config.json`，找不到则 fallback 到 `config-example.json`（API Key 为占位符，步骤4 会失败）。

## 输出结构

```
output/
├── transition/项目名/集数文件夹.mp4      # 步骤3 转场拼接后（无字幕）
└── final/项目名/集数文件夹.mp4           # 步骤4 字幕烧录后的最终视频

scripts/output/subtitle/项目名/集数文件夹/  # 本地产物，已 gitignore
├── xxx_qwen3_optimized.srt              # 字幕文件
├── xxx_qwen3_optimized.json             # 字幕元数据
└── video/集数文件夹.mp4                 # 带字幕视频（final/ 的来源）

temp_output/项目名/集数文件夹/
├── 1_trimmed.mp4                        # 步骤1 黑屏裁剪后
├── 1_trimmed.wav                        # 步骤2 分离用音频
└── 1_trimmed_vocals.mp4                 # 步骤2 人声替换后
```

## 单独运行各步骤

```bash
# 步骤1: 黑屏检测
python scripts/001remove_black.py video.mp4 -o temp_output/

# 步骤2: 声道分离
conda run -n msst python scripts/002separate.py video.mp4 temp_output/

# 步骤3: 转场拼接
python scripts/003transition_simple.py --folder assets/项目名/集数文件夹 -o output.mp4 --bgm bgm.flac

# 步骤4: 字幕生成与烧录
python scripts/004subtitles.py --video output.mp4 --config config.json
```

## 常见问题

**声道分离失败**

```bash
conda activate msst
python -c "import torch; print(torch.cuda.is_available())"
```

返回 `False` 说明 PyTorch 未识别到 GPU，需重新安装 CUDA 版本的 PyTorch。

**字幕生成失败**

- 确认 Qwen3-ASR 推理服务已启动（`tools/qwen3-asr-deployment/`）
- 确认 `config.json` 中 `kimi_api_key` 有效
- 确认模型文件在 `libs/Qwen3-ASR/models/Qwen3-ASR-1.7B/`

**显存不足**

步骤2（声道分离）显存占用较高，建议 8GB+ 显存。可先用 `--skip 2` 跳过测试其他步骤。

## 许可证

本项目基于 [MIT License](LICENSE)。

集成的第三方开源工具，请遵守各自许可证：
- [Music-Source-Separation-Training](https://github.com/ZFTurbo/Music-Source-Separation-Training)
- [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR)

## 贡献

欢迎提交 Issue 和 Pull Request。
