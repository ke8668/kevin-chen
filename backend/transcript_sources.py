"""
Transcript Sources
====================
集中管理「自動抓取逐字稿」的不同來源。每個來源都是獨立函式，
回傳統一格式: List[{"t": float, "en": str, "zh": str}]

目前實作：
  - YouTube 字幕（youtube-transcript-api，免費、不需 API key）
    + 免費翻譯（googletrans，非官方、不保證穩定）

預留、尚未實作：
  - Substack newsletter 的逐字稿解析（需要執行 JS 才能拿到內容，
    之後若要做，建議用 Playwright headless browser，但會大幅
    增加部署複雜度與資源需求，故先留空函式）

注意事項（請讓使用者知道的限制）：
  - YouTube 自動字幕本身就可能有辨識錯誤（不是人工校對的逐字稿）
  - googletrans 是透過逆向工程 Google 網頁版翻譯的非官方函式庫，
    Google 隨時可能改變機制讓它失效，也可能因為請求量被暫時封鎖
  - 這兩個來源都「沒有」官方 SLA，請預期偶爾會失敗，並提供
    清楚的錯誤訊息讓使用者知道發生了什麼事，而不是默默壞掉
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import List, Optional

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
)


@dataclass
class TranscriptLine:
    t: float
    en: str
    zh: str = ""

    def to_dict(self) -> dict:
        return {"t": round(self.t, 2), "en": self.en, "zh": self.zh}


class TranscriptSourceError(Exception):
    """所有逐字稿來源共用的錯誤類型，附帶人類可讀訊息。"""
    pass


# --------------------------------------------------------------------------
# YouTube 影片 ID 解析
# --------------------------------------------------------------------------

YOUTUBE_ID_PATTERNS = [
    r"(?:youtube\.com/watch\?v=|youtube\.com/embed/|youtu\.be/)([A-Za-z0-9_-]{11})",
    r"^([A-Za-z0-9_-]{11})$",  # 使用者直接給 11 字元的 video id
]


def extract_youtube_video_id(url_or_id: str) -> str:
    """從 YouTube 連結（多種格式）或純 video id 字串中取出 11 字元的 video id"""
    url_or_id = url_or_id.strip()
    for pattern in YOUTUBE_ID_PATTERNS:
        m = re.search(pattern, url_or_id)
        if m:
            return m.group(1)
    raise TranscriptSourceError(
        "無法從這個輸入辨識出 YouTube 影片 ID，請確認貼上的是完整的 YouTube 連結"
        "（例如 https://www.youtube.com/watch?v=xxxxxxxxxxx）或 11 字元的影片 ID。"
    )


# --------------------------------------------------------------------------
# YouTube 字幕抓取
# --------------------------------------------------------------------------

def fetch_youtube_transcript(
    video_url_or_id: str,
    preferred_languages: Optional[List[str]] = None,
) -> List[TranscriptLine]:
    """
    抓取 YouTube 影片的字幕（人工上傳優先，沒有則用自動生成字幕）。
    回傳的 TranscriptLine.zh 一律是空字串，翻譯由另一個函式負責，
    讓「抓字幕」跟「翻譯」這兩個容易各自失敗的步驟可以分開除錯。
    """
    video_id = extract_youtube_video_id(video_url_or_id)
    languages = preferred_languages or ["en", "en-US", "en-GB"]

    api = YouTubeTranscriptApi()
    try:
        fetched = api.fetch(video_id, languages=languages)
    except TranscriptsDisabled:
        raise TranscriptSourceError(
            "這部影片的字幕功能被上傳者關閉了，無法取得逐字稿。"
        )
    except NoTranscriptFound:
        raise TranscriptSourceError(
            "這部影片沒有英文字幕（也沒有自動生成字幕），無法取得逐字稿。"
        )
    except VideoUnavailable:
        raise TranscriptSourceError(
            "找不到這部影片，可能已被刪除、設為不公開，或影片 ID 不正確。"
        )
    except Exception as e:
        raise TranscriptSourceError(f"抓取 YouTube 字幕時發生未預期錯誤：{e}")

    lines = [TranscriptLine(t=snippet.start, en=snippet.text.strip())
             for snippet in fetched if snippet.text.strip()]

    if not lines:
        raise TranscriptSourceError("抓到字幕資料，但內容是空的。")

    return lines


# --------------------------------------------------------------------------
# 免費翻譯（googletrans，非官方）
# --------------------------------------------------------------------------

# 一次翻譯太多句容易被 Google 限流或逾時，所以分批、批次之間加小延遲
_TRANSLATE_BATCH_SIZE = 20
_TRANSLATE_BATCH_DELAY_SECONDS = 0.5


async def translate_lines_to_zh(lines: List[TranscriptLine]) -> List[TranscriptLine]:
    """
    用免費、非官方的 googletrans 把每一句英文翻成中文。
    這個函式會盡力翻譯每一句；單句翻譯失敗不會讓整批失敗，
    只會讓那一句的 zh 保持空字串（前端會顯示「翻譯失敗」之類的提示）。
    """
    try:
        from googletrans import Translator
    except ImportError:
        raise TranscriptSourceError(
            "後端尚未安裝翻譯套件（googletrans），請執行 "
            "pip install googletrans==4.0.0-rc1 後再試。"
        )

    translator = Translator()
    total = len(lines)

    for batch_start in range(0, total, _TRANSLATE_BATCH_SIZE):
        batch = lines[batch_start: batch_start + _TRANSLATE_BATCH_SIZE]
        for line in batch:
            try:
                result = await translator.translate(line.en, src="en", dest="zh-tw")
                line.zh = result.text
            except Exception:
                # 單句翻譯失敗時保留空字串，不中斷整個流程
                line.zh = ""
        if batch_start + _TRANSLATE_BATCH_SIZE < total:
            await asyncio.sleep(_TRANSLATE_BATCH_DELAY_SECONDS)

    return lines


# --------------------------------------------------------------------------
# Substack 逐字稿解析（預留接口，尚未實作）
# --------------------------------------------------------------------------

def fetch_substack_transcript(article_url: str) -> List[TranscriptLine]:
    """
    預留接口：之後若要支援 Substack newsletter 的 Transcript 區塊，
    實作時請注意：

      1. Substack 的逐字稿內容是透過前端 JavaScript 動態載入的，
         單純用 httpx/requests 抓 HTML 通常拿不到內容。
      2. 可行的做法是用 Playwright（headless Chromium）打開頁面、
         等待逐字稿區塊渲染完成、再抓取 DOM 內容。
      3. 這會讓部署變重：Render 免費方案的記憶體可能不足以穩定
         跑 headless 瀏覽器，需要評估是否要換成付費方案或別的
         無頭瀏覽器服務（例如 Browserless）。
      4. 抓到的逐字稿是原作者的版權內容，請只用於個人學習用途，
         不要公開散布轉換後的字幕檔。

    目前呼叫這個函式一律會丟出「尚未實作」的錯誤，讓前端可以
    明確告知使用者，而不是默默回傳空結果。
    """
    raise TranscriptSourceError(
        "Substack 逐字稿自動解析尚未實作（需要 headless 瀏覽器支援，"
        "之後會視部署資源狀況再加上）。目前請改用 YouTube 字幕來源，"
        "或手動複製貼上後自行整理成 JSON 字幕檔。"
    )
