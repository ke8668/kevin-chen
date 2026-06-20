# Bilingo Podcast Backend

完全免費的後端，負責「把 Apple Podcasts 連結轉換成真實可播放的 mp3 網址」。
不呼叫任何要付費的 API（不含語音辨識、不含翻譯），所以沒有任何使用成本。

## 這個後端做的事

1. 解析你貼的 Apple Podcasts 連結，取出 podcast id（與單集 id，如果有）
2. 呼叫 iTunes Lookup API（Apple 官方公開、免費）取得該節目的 RSS Feed 網址
3. 抓取並解析 RSS Feed（podcast 背後一定有一個公開的 RSS，這是業界標準）
4. 在 RSS 裡找出對應的單集，回傳它的真實 mp3 串流網址、標題、時長、封面
5. 提供 `/api/episode-by-title`，當 Apple 連結裡的單集 id 跟 RSS 對不上時，
   可以用標題關鍵字手動比對到正確的那一集

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
4. Render 會自動讀到根目錄的 `render.yaml`，設定會自動填好：
   - Root Directory: `backend`
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. 選擇 **Free** 方案，按 "Create Web Service"
6. 等待部署完成後，你會拿到一個網址，例如：
   `https://bilingo-podcast-backend.onrender.com`

把這個網址填進前端的「後端 API 網址」欄位，就能用了。

### 免費方案的限制（老實說清楚）

- 服務閒置一段時間後會「睡著」，下次請求要等 30–60 秒喚醒（之後請求就正常快）
- 沒有固定的記憶體/CPU 保證，流量大時可能變慢
- 這些限制只影響「查詢 podcast 資訊」這個步驟，不影響音訊播放本身
  （播放時瀏覽器是直接連到 Apple/節目方的 CDN，不經過這個後端）

## 重要限制：Apple 連結裡的單集 id 不一定對得上 RSS

Apple Podcasts 網址裡的 `?i=1000769854268` 是 **Apple 內部的單集 ID**，
跟節目自己 RSS Feed 裡的 `<guid>` **不一定是同一套編號系統**。

這個後端會盡力比對（很多節目兩者剛好一致，本專案測試的
《The Pragmatic Engineer》就是如此），但如果比對不到，
`matched_episode` 會是 `null`，這時：

- 前端會顯示「最近單集清單」(`recent_episodes`)，讓你直接點選正確的那一集，或
- 呼叫 `/api/episode-by-title?url=...&title_contains=關鍵字` 用標題比對

## 之後想接語音辨識／翻譯（會產生費用）

目前字幕是由你手動上傳 SRT / VTT / JSON 檔，完全在前端瀏覽器解析，
不經過這個後端。如果之後想要「自動」產生逐句字幕＋翻譯：

- 在 `main.py` 新增一個 `/api/transcribe` endpoint
- 下載 `audio_url` 指向的 mp3
- 呼叫 Whisper API（或其他語音辨識服務）取得逐句時間碼
- 呼叫翻譯 API（GPT 或其他）把每句英文翻成中文
- 回傳跟前端現有的 `lines: [{t, en, zh}]` 格式相同的資料

這樣前端完全不需要改動，只要把「手動上傳字幕」的按鈕换成「自動產生字幕」即可。
