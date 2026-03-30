Qwen3-ASR 部署包
================

本目录用于在本地或服务器上部署 Qwen3-ASR 语音识别推理服务，供 scripts/003subtitles.py 调用。

快速开始:
1. 安装依赖: pip install -r requirements_server.txt
2. 下载模型文件（约 4.6GB）放入 libs/Qwen3-ASR/models/Qwen3-ASR-1.7B/
   下载地址: https://huggingface.co/Qwen/Qwen3-ASR-1.7B
3. 运行安装脚本: bash install.sh

文件说明:
- install.sh                   自动安装脚本
- requirements_server.txt      Python 依赖（transformers、librosa 等）

注意事项:
- 模型文件约 4.6GB，需要单独下载，不含在本仓库中
- 推荐使用 GPU 服务器（RTX 3060 以上）
- CPU 模式处理速度较慢（约 13 分钟/分钟视频）

技术支持:
- Qwen3-ASR 官方仓库: https://github.com/QwenLM/Qwen3-ASR
- Hugging Face 模型页: https://huggingface.co/Qwen/Qwen3-ASR-1.7B
