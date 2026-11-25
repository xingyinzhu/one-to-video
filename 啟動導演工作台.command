#!/bin/bash
# ==============================================================================
# AI 紀錄片導演工作台（One-Sentence to Video Web App）macOS 雙擊一鍵啟動腳本
# ==============================================================================

set -e

# 切換至腳本所在專案目錄（完整支援含中文或空格的路徑）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 補齊 macOS Homebrew 常見執行檔路徑（避免從 Finder 雙擊時 PATH 不含 brew）
export PATH="/opt/homebrew/bin:/usr/local/bin:/opt/local/bin:$PATH"

PORT="${STUDIO_PORT:-8787}"
HOST="${STUDIO_HOST:-127.0.0.1}"
URL="http://${HOST}:${PORT}"

echo "============================================================"
echo "🎬 AI 紀錄片導演工作台（One-Sentence to Video）啟動中..."
echo "📂 工作目錄：$SCRIPT_DIR"
echo "============================================================"

# 1. 檢查 Python 3
if ! command -v python3 >/dev/null 2>&1; then
  echo "❌ 找不到 Python 3！請先安裝 Python 3.10 以上版本（例如執行：brew install python）"
  read -r -p "按 Enter 鍵關閉視窗..."
  exit 1
fi

# 2. 建立並啟動虛擬環境 .venv
if [ ! -x ".venv/bin/python" ]; then
  echo "📦 首次啟動：正在建立獨立 Python 虛擬環境 (.venv)..."
  python3 -m venv .venv
fi

# 3. 檢查並安裝套件依賴
if ! .venv/bin/python -c "import fastapi, uvicorn, watchdog, httpx, colab_cli" >/dev/null 2>&1; then
  echo "⬇️  正在安裝必要執行套件（含官方 google-colab-cli，僅首次需要約 15～30 秒）..."
  .venv/bin/pip install --upgrade pip >/dev/null 2>&1 || true
  .venv/bin/pip install -r requirements.txt
fi

# 4. 檢查 FFmpeg（支援系統 Homebrew 或內建 static-ffmpeg 自動備援）
echo "🔍 檢查 FFmpeg 影音剪輯引擎..."
FFMPEG_CHECK=$(.venv/bin/python -c "from backend.settings import detect_ffmpeg_binaries; f1, f2 = detect_ffmpeg_binaries(allow_static_download=True); print('OK' if (f1 and f2) else 'MISSING')")

if [ "$FFMPEG_CHECK" != "OK" ]; then
  echo "⚠️  尚未偵測到系統 FFmpeg！建議在終端機執行以下指令安裝："
  echo "    brew install ffmpeg"
fi

# 5. 若連接埠已被既有工作台佔用，直接開啟瀏覽器即可
if lsof -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo "✅ 偵測到導演工作台已在背景執行中！正在為您打開瀏覽器：$URL"
  open "$URL"
  exit 0
fi

# 6. 背景等待伺服器就緒後自動開啟瀏覽器
(
  for _ in $(seq 1 30); do
    if curl -s "$URL/api/system/status" >/dev/null 2>&1; then
      open "$URL"
      exit 0
    fi
    sleep 0.4
  done
  open "$URL"
) &

echo "🚀 服務啟動完成！瀏覽器將自動開啟：$URL"
echo "💡 使用期間請保留此視窗縮小於背景；欲結束程式請按 Ctrl + C 或直接關閉視窗。"
echo "------------------------------------------------------------"

exec .venv/bin/python -m uvicorn backend.main:app --host "$HOST" --port "$PORT"
