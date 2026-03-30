#!/bin/bash
# Qwen3-ASR 自动安装脚本

echo "======================================"
echo "Qwen3-ASR 自动安装"
echo "======================================"

# 检查 Python 版本
echo ""
echo "检查 Python 版本..."
python_version=$(python3 --version 2>&1 | awk '{print $2}')
echo "Python 版本: $python_version"

# 检查 CUDA
echo ""
echo "检查 CUDA..."
if command -v nvidia-smi &> /dev/null; then
    echo "✓ 检测到 NVIDIA GPU"
    nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
    USE_GPU=true
else
    echo "⚠️  未检测到 NVIDIA GPU，将使用 CPU"
    USE_GPU=false
fi

# 创建虚拟环境
echo ""
echo "创建虚拟环境..."
if command -v conda &> /dev/null; then
    echo "使用 Conda 创建环境..."
    conda create -n qwen3-asr python=3.12 -y
    echo "请运行: conda activate qwen3-asr"
else
    echo "使用 venv 创建环境..."
    python3 -m venv qwen3-asr-env
    echo "请运行: source qwen3-asr-env/bin/activate"
fi

# 安装 PyTorch
echo ""
echo "安装 PyTorch..."
if [ "$USE_GPU" = true ]; then
    echo "安装 GPU 版本..."
    pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
else
    echo "安装 CPU 版本..."
    pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu
fi

# 安装 Qwen3-ASR
echo ""
echo "安装 Qwen3-ASR..."
cd Qwen3-ASR
pip install -e .
cd ..

# 安装 FlashAttention (可选)
if [ "$USE_GPU" = true ]; then
    echo ""
    read -p "是否安装 FlashAttention 2? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "安装 FlashAttention 2..."
        pip install flash-attn --no-build-isolation
    fi
fi

# 下载模型
echo ""
echo "======================================"
echo "模型下载"
echo "======================================"
echo ""
echo "请选择下载方式:"
echo "1) ModelScope (推荐国内服务器)"
echo "2) Hugging Face"
echo "3) 跳过 (稍后手动下载)"
read -p "请选择 (1/2/3): " choice

case $choice in
    1)
        echo "使用 ModelScope 下载..."
        pip install modelscope
        
        echo "下载 ASR 模型..."
        modelscope download --model Qwen/Qwen3-ASR-1.7B \
            --local_dir ./Qwen3-ASR/models/Qwen3-ASR-1___7B
        
        echo "下载对齐模型..."
        modelscope download --model Qwen/Qwen3-ForcedAligner-0.6B \
            --local_dir ./Qwen3-ASR/models/Qwen3-ForcedAligner-0___6B
        ;;
    2)
        echo "使用 Hugging Face 下载..."
        pip install huggingface_hub
        
        echo "下载 ASR 模型..."
        huggingface-cli download Qwen/Qwen3-ASR-1.7B \
            --local-dir ./Qwen3-ASR/models/Qwen3-ASR-1___7B
        
        echo "下载对齐模型..."
        huggingface-cli download Qwen/Qwen3-ForcedAligner-0.6B \
            --local-dir ./Qwen3-ASR/models/Qwen3-ForcedAligner-0___6B
        ;;
    3)
        echo "跳过模型下载"
        echo "请参考 DEPLOYMENT_GUIDE.md 手动下载模型"
        ;;
esac

echo ""
echo "======================================"
echo "✓ 安装完成！"
echo "======================================"
echo ""
echo "下一步:"
echo "1. 激活虚拟环境"
echo "2. 上传视频文件"
echo "3. 运行: python generate_subtitles_qwen3_optimized.py"
echo ""
