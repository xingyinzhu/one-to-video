# ==============================================================================
# AI 紀錄片導演工作台 (One-Sentence to Video Web App) Production Dockerfile
# 支援：macOS / Linux / Windows Docker (linux/amd64 & linux/arm64)
# ==============================================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STUDIO_HOST=0.0.0.0 \
    STUDIO_PORT=8787 \
    STUDIO_DATA_DIR=/app/data \
    STUDIO_DOWNLOADS_DIR=/downloads \
    STUDIO_DESKTOP_OUTPUT_DIR=/outputs

# 安裝系統級 FFmpeg / FFprobe 與健康檢查工具 curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先安裝 Python 依賴以最大化 Docker Layer 快取效率
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

# 複製應用程式原始碼與 Colab 筆記本
COPY backend/ /app/backend/
COPY frontend/ /app/frontend/
COPY colab/ /app/colab/
COPY pytest.ini README.md /app/

# 建立掛載與資料持久化目錄
RUN mkdir -p /app/data /outputs /downloads /root/.config/colab-cli

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8787/api/system/status || exit 1

CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8787"]
