# Bilingo Podcast Backend

完全免費的後端（不需要任何付費 API key），負責兩件事：

1. 把 Apple Podcasts 連結轉換成真實可播放的 mp3 網址
2. （新增）從 YouTube 影片自動抓取字幕，並用免費翻譯服務轉成中文

## 這個後端做的事

### A. Podcast 解析

1. 解析你貼的 Apple Podcasts 連結，取出 podcast id（與單集 id，如果有）
2. 呼叫 iTunes Lookup API（Apple 官方公開、免費）取得該節目的 RSS Feed 網址
3. 抓取並解析 RSS Feed（podcast 背後一定有一個公開的 RSS，這是業界標準）
4. 在 RSS 裡找出對應的單集，回傳它的真實 mp3 串流網址、標題、時長、封面
5. 提供 `/api/episode-by-title`，當 Apple 連結裡的單集 id 跟 RSS 對不上時，
   可以用標題關鍵字手動比對到正確的那一集

### B. 自動抓取逐字稿（新增）

- `/api/transcript/youtube`：給一個 YouTube 影片連結，抓取該影片的字幕
  （人工上傳或自動生成皆可），並（可選）用免費翻譯服務翻成中文
- `/api/transcript/substack`：**尚未實作**，目前一律回傳 501。
  Substack newsletter 的逐字稿是用 JavaScript 動態載入的，
  要抓到內容需要 headless 瀏覽器（例如 Playwright），這會讓部署
  變重很多（Render 免費方案的記憶體可能不夠用），所以先擱置。
  程式碼裡已經留好函式接口（`transcript_sources.py` 的
  `fetch_substack_transcript`），之後要實作時邏輯加進那個函式即可，
  不需要改動其他地方。

## 本機執行

```bash
cd backend
python3 -m venv venv
source venv/bin/activate        # Windows 用 venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

啟動後打開 http://127.0.0.1:8000/docs 可以看到自動產生的 API 文件，直接在網頁上測試。

## 部署到 Render.com（免費方案）

1. 把這個專案推到你自己的 GitHub repo
2. 到 https://render.com 註冊（免費方案不需要信用卡）
3. 點 "New +" → "Web Service"，選擇你的 repo
4. 手動填入（或讓 Render 讀取根目錄的 `render.yaml`）：
   - Root Directory: `backend`
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Environment Variables：**不需要填任何東西**，這個專案不依賴任何
   需要密鑰的服務
6. 選擇 **Free** 方案，按 "Create Web Service"
7. 等待部署完成後，你會拿到一個網址，例如：
   `https://your-app-name.onrender.com`

把這個網址填進前端的「後端 API 網址」欄位，就能用了。

### 免費方案的限制（老實說清楚）

- 服務閒置一段時間後會「睡著」，下次請求要等 30–60 秒喚醒（之後請求就正常快）
- 沒有固定的記憶體/CPU 保證，流量大時可能變慢
- 這些限制只影響「查詢 podcast 資訊 / 抓字幕」這幾個步驟，
  不影響音訊播放本身（播放時瀏覽器是直接連到 Apple/節目方的 CDN，
  不經過這個後端）

## 重要限制：Apple 連結裡的單集 id 不一定對得上 RSS

Apple Podcasts 網址裡的 `?i=1000769854268` 是 **Apple 內部的單集 ID**，
跟節目自己 RSS Feed 裡的 `<guid>` **不一定是同一套編號系統**。

這個後端會盡力比對（很多節目兩者剛好一致，本專案測試的
《The Pragmatic Engineer》就是如此），但如果比對不到，
`matched_episode` 會是 `null`，這時：

- 前端會顯示「最近單集清單」(`recent_episodes`)，讓你直接點選正確的那一集，或
- 呼叫 `/api/episode-by-title?url=...&title_contains=關鍵字` 用標題比對

## YouTube 字幕功能的限制（請務必先知道）

這個功能完全免費，但代價是「不保證穩定」，請理解這個取捨：

- **抓字幕**：用開源套件 `youtube-transcript-api`。如果該影片完全
  沒有字幕（人工或自動生成都沒有），或字幕功能被上傳者關閉，會抓不到。
- **翻譯**：用 `googletrans-py`（非官方、免費的 Google 翻譯逆向工程
  套件）。這個套件透過模擬 Google 翻譯網頁版的請求運作，**不是官方
  API**，代表：
  - Google 隨時可能改變機制讓它失效
  - 翻譯量大或頻繁呼叫時，可能被暫時限流或封鎖（一段時間後通常恢復）
  - 單句翻譯失敗時，那一句的中文會留空，但不會讓整批請求失敗
    （你可以之後手動補上那幾句）
- 自動字幕本身（不論英文還是翻譯後的中文）都可能有辨識或翻譯誤差，
  品質比不上人工逐字稿，請當作「快速產生初稿」用，重要內容建議再人工校對。

如果這個免費路線不夠穩定，之後若想要更穩定的版本，可以考慮：
- 換成官方 Google Cloud Translation API（需要付費，但有 SLA）
- 換成 OpenAI / DeepL 等翻譯 API（需要付費）
- 程式碼結構已經把「抓字幕」跟「翻譯」分成兩個獨立函式
  （`transcript_sources.py` 裡的 `fetch_youtube_transcript` 與
  `translate_lines_to_zh`），之後要換掉翻譯服務，只需要改
  `translate_lines_to_zh` 這一個函式，不影響其他部分。

## 想自己接 Whisper 語音辨識（會產生費用，目前未實作）

如果之後想要「完全自動」處理沒有 YouTube 對應版本、也沒有公開逐字稿
的 podcast：

- 在 `main.py` 新增一個 `/api/transcribe` endpoint
- 下載 `audio_url` 指向的 mp3
- 呼叫 Whisper API（或其他語音辨識服務）取得逐句時間碼
- 沿用現有的 `translate_lines_to_zh` 來翻譯
- 回傳跟前端現有的 `lines: [{t, en, zh}]` 格式相同的資料

這樣前端完全不需要改動。
