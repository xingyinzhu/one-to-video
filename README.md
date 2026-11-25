# 🎬 AI 紀錄片導演工作台（One-Sentence to Video Web App）

只要寫**一句簡單的話**（例如：「倫敦聖誕節前的攝政街」），本機 Web App 透過 **Google 官方 `google-colab-cli`** 自動開機搶卡（`G4 ➔ A100 ➔ L4`）、自動部署 ComfyUI ＋ 開源 **MiniMax H3** 首尾幀接力生片、自動拉回 Mac 合成 `1920×1080` 成片至桌面，並**在拍完瞬間自動執行 `colab stop` 關機停止扣計算單元**——真正做到 **\$0 額外商業影片 API 費、0 人工開 Colab 網頁、0 手動下載**！

---

## ⚡ 終極零人工全自動使用方式（僅首次授權 1 次！）

### 首次設定（只需做這一次，約 30 秒）
1. 在 Finder 雙擊 **[啟動導演工作台.command](./啟動導演工作台.command)**，瀏覽器會自動打開 `http://127.0.0.1:8787`。
   *(若首次雙擊出現 macOS 安全性提示，請對檔案按**右鍵 ➔「打開」**，之後每次皆可直接雙擊秒開。)*
2. 點擊畫面左下角 **「🟠 未授權 Colab（點此綁定）」**（或階段 ③ 的 **「🔑 授權 Google Colab」**）：
   - 按 **「🌐 開啟 Google 官方授權頁面」** ➔ 登入你的 Google Colab Pro 帳號並點允許。
   - 把頁面顯示的授權碼貼回工作台，點 **「✅ 完成永久授權」**（安全存入你 Mac 本機鑰匙圈，以後永遠不用重複登入！）。

### 日常拍片（真正「寫一句話 ➔ 按一鍵 ➔ 等收成片」）
1. 在階段 ① 輸入你想拍的一句話（例如「倫敦聖誕節前的攝政街」），選好時長與是否無聲。
2. 點擊橘色大按鈕 **「⚡ 一句話終極全自動（自動開 Colab ➔ 生片 ➔ 合成 ➔ 自動關機）」**（或先點「先拆解分鏡再確認」微調完分鏡再到階段 ③ 按一鍵全自動）。
3. **接下來完全不用管它！** 系統會全程自動：
   - ☁️ 依序搶卡開機（`G4 96GB ➔ A100 80GB ➔ L4 24GB` 缺卡自動無縫降級）
   - 📦 雲端部署 ComfyUI ＋ MiniMax H3 並自動擷取連線網址
   - 🎬 逐幕首尾幀接力生成、拉回 Mac 產生即時縮圖
   - 🛑 **一收齊影片立刻自動 `colab stop` 關機省算力**（失敗也會保證關機！）
   - 🎞️ 本機 FFmpeg 合成 `1920×1080`、`24fps` 電影成片並直接存到 **`~/Desktop/AutoVideoOutputs/`**！

---

## 🐳 Docker 一鍵啟動（支援 macOS / Linux / Windows / NAS）

本專案已發布官方 Docker Hub 映像檔：**`xingyinzhu/one-to-video:latest`**（內建 FFmpeg、FastAPI 與官方 `google-colab-cli`，並自動相容容器無頭 OAuth 憑證持久化）：

### 方式 A：使用 Docker Compose（推薦）
```bash
docker compose up -d
```
開啟瀏覽器前往 **`http://127.0.0.1:8787`** 即可使用，所有專案資料、成片輸出與 Colab 授權皆自動持久化於本機 `./data`、`./outputs`、`./colab-config`。

### 方式 B：單行 `docker run` 直接拉取啟動
```bash
docker run -d --name one-to-video \
  -p 8787:8787 \
  -v "$(pwd)/data:/app/data" \
  -v "$(pwd)/outputs:/outputs" \
  -v "$(pwd)/downloads:/downloads" \
  -v "$(pwd)/colab-config:/root/.config/colab-cli" \
  xingyinzhu/one-to-video:latest
```

---

## 🧪 執行自動化端對端測試

```bash
./.venv/bin/pytest tests/test_end_to_end.py -v
```
