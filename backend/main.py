"""
Bilingo Podcast Backend
========================
免費路線（不呼叫任何付費 API）：

  使用者貼上 Apple Podcasts 連結
        -> 解析出 podcast id (id=) 與可選的單集 id (i=)
        -> 呼叫 iTunes Lookup API 取得 RSS Feed 網址
        -> 抓取並解析 RSS feed
        -> 找到對應單集（或回傳整個節目的單集清單供前端選擇）
        -> 回傳該集標題、封面、時長、真實 mp3 串流網址

字幕（逐句雙語）目前由使用者手動上傳 SRT / VTT / JSON 檔，
完全在前端瀏覽器解析，不經過後端、不需要任何 API key。

之後若要接語音辨識（Whisper）+ 翻譯，只需要新增
/transcribe 這個 endpoint，不需更動現有架構。
"""

from __future__ import annotations

import re
import time
from typing import Optional, List
from urllib.parse import urlparse, parse_qs

import feedparser
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from transcript_sources import (
    fetch_youtube_transcript,
    translate_lines_to_zh,
    fetch_substack_transcript,
    TranscriptSourceError,
)

app = FastAPI(title="Bilingo Podcast Backend", version="0.1.0")

# 開發階段先全部放開；之後部署時應改成你前端的實際網域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

import os

ITUNES_LOOKUP_URL = os.environ.get("ITUNES_LOOKUP_URL", "https://itunes.apple.com/lookup")

# 簡單的記憶體快取，避免短時間內重複打 iTunes / RSS
_cache: dict[str, tuple[float, dict]] = {}
CACHE_TTL_SECONDS = 60 * 30  # 30 分鐘


# --------------------------------------------------------------------------
# Pydantic models
# --------------------------------------------------------------------------

class EpisodeOut(BaseModel):
    episode_id: Optional[str] = None
    title: str
    audio_url: str
    duration_seconds: Optional[int] = None
    published: Optional[str] = None
    cover_art: Optional[str] = None


class PodcastOut(BaseModel):
    podcast_id: str
    podcast_title: str
    cover_art: Optional[str] = None
    feed_url: str
    # 若連結指向特定單集，這裡會是該集；否則為 None
    matched_episode: Optional[EpisodeOut] = None
    # 最近的單集清單（供前端讓使用者挑選）
    recent_episodes: List[EpisodeOut] = []


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def parse_apple_podcast_url(url: str) -> tuple[str, Optional[str]]:
    """
    從 Apple Podcasts 連結解析出 (podcast_id, episode_id)。

    範例：
    https://podcasts.apple.com/tw/podcast/the-pragmatic-engineer/id1769051199?i=1000769854268
      -> podcast_id = "1769051199", episode_id = "1000769854268"

    https://podcasts.apple.com/us/podcast/some-show/id123456789
      -> podcast_id = "123456789", episode_id = None
    """
    parsed = urlparse(url)
    if "apple.com" not in parsed.netloc:
        raise ValueError("這不是 Apple Podcasts 的連結")

    # podcast id 通常在路徑中以 idXXXXXXXXX 出現
    match = re.search(r"/id(\d+)", parsed.path)
    if not match:
        raise ValueError("找不到 podcast id（網址格式不正確）")
    podcast_id = match.group(1)

    query = parse_qs(parsed.query)
    episode_id = query.get("i", [None])[0]

    return podcast_id, episode_id


async def fetch_itunes_feed_url(podcast_id: str, country: str = "tw") -> tuple[str, str, Optional[str]]:
    """
    呼叫 iTunes Lookup API，回傳 (feed_url, podcast_title, cover_art)
    """
    params = {"id": podcast_id, "country": country, "entity": "podcast"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(ITUNES_LOOKUP_URL, params=params)
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"iTunes Lookup API 回應失敗（狀態碼 {resp.status_code}）",
        )
    data = resp.json()
    results = data.get("results", [])
    if not results:
        raise HTTPException(
            status_code=404,
            detail="iTunes 上找不到這個 podcast id，請確認連結是否正確，或換一個 country 參數",
        )
    item = results[0]
    feed_url = item.get("feedUrl")
    if not feed_url:
        raise HTTPException(
            status_code=404,
            detail="這個節目在 iTunes 上沒有公開 RSS Feed 網址（可能是 Apple 獨家節目，無法用此方式取得音訊）",
        )
    title = item.get("collectionName", "未知節目")
    cover_art = item.get("artworkUrl600") or item.get("artworkUrl100")
    return feed_url, title, cover_art


def _entry_to_episode(entry) -> Optional[EpisodeOut]:
    """將 feedparser 的單一 entry 轉換成 EpisodeOut，找不到音訊則回傳 None"""
    audio_url = None
    for link in getattr(entry, "links", []):
        if link.get("type", "").startswith("audio") or link.get("rel") == "enclosure":
            audio_url = link.get("href")
            break
    if not audio_url and hasattr(entry, "enclosures") and entry.enclosures:
        audio_url = entry.enclosures[0].get("href")
    if not audio_url:
        return None

    duration_seconds = None
    raw_duration = getattr(entry, "itunes_duration", None)
    if raw_duration:
        duration_seconds = _parse_duration_to_seconds(raw_duration)

    episode_id = None
    guid = getattr(entry, "id", None) or getattr(entry, "guid", None)
    if guid:
        episode_id = str(guid)

    cover = None
    if hasattr(entry, "image") and isinstance(entry.image, dict):
        cover = entry.image.get("href")

    return EpisodeOut(
        episode_id=episode_id,
        title=getattr(entry, "title", "未知標題"),
        audio_url=audio_url,
        duration_seconds=duration_seconds,
        published=getattr(entry, "published", None),
        cover_art=cover,
    )


def _parse_duration_to_seconds(raw: str) -> Optional[int]:
    """把 itunes:duration 常見的 'HH:MM:SS' / 'MM:SS' / 純秒數 字串轉成秒數"""
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    parts = raw.split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return None
    if len(parts) == 3:
        h, m, s = parts
        return h * 3600 + m * 60 + s
    if len(parts) == 2:
        m, s = parts
        return m * 60 + s
    return None


def _get_cached(key: str) -> Optional[dict]:
    item = _cache.get(key)
    if not item:
        return None
    ts, value = item
    if time.time() - ts > CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return None
    return value


def _set_cached(key: str, value: dict) -> None:
    _cache[key] = (time.time(), value)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/resolve", response_model=PodcastOut)
async def resolve_podcast(
    url: str = Query(..., description="Apple Podcasts 的節目或單集連結"),
    country: str = Query("tw", description="iTunes Store 國家代碼，例如 tw / us"),
    episode_limit: int = Query(15, ge=1, le=50, description="回傳最近幾集供選擇"),
):
    """
    輸入一個 Apple Podcasts 連結，回傳：
      - 節目資訊（標題、封面、RSS feed 網址）
      - 若連結含單集 id，嘗試比對出該集的真實 mp3 網址
      - 最近 N 集的清單（含 mp3 網址），供使用者直接挑選
    """
    cache_key = f"{url}::{country}::{episode_limit}"
    cached = _get_cached(cache_key)
    if cached:
        return cached

    try:
        podcast_id, episode_id = parse_apple_podcast_url(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    feed_url, podcast_title, cover_art = await fetch_itunes_feed_url(podcast_id, country)

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        try:
            resp = await client.get(feed_url, headers={"User-Agent": "BilingoPodcastApp/0.1"})
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"無法抓取 RSS Feed：{e}")

    parsed_feed = feedparser.parse(resp.content)
    if parsed_feed.bozo and not parsed_feed.entries:
        raise HTTPException(status_code=502, detail="RSS Feed 解析失敗，格式可能不支援")

    episodes: List[EpisodeOut] = []
    matched_episode: Optional[EpisodeOut] = None

    for entry in parsed_feed.entries[:max(episode_limit, 30)]:
        ep = _entry_to_episode(entry)
        if not ep:
            continue
        if not ep.cover_art:
            ep.cover_art = cover_art
        episodes.append(ep)

        # 嘗試用標題粗略比對單集（RSS guid 跟 Apple 的 episode id 不一定一致，
        # 所以這裡先用「最新一集」當預設，真正比對見下方 fallback 區塊）
        if episode_id and episode_id in (ep.episode_id or ""):
            matched_episode = ep

        if len(episodes) >= episode_limit:
            break

    # Apple 的 i= 數字常常對不上 RSS 裡的 guid，這裡退而求其次：
    # 若沒比對到，先回傳清單，讓前端用「集數標題」給使用者自己挑選，
    # 而不是默默猜錯導致播放錯誤的單集。
    result = PodcastOut(
        podcast_id=podcast_id,
        podcast_title=podcast_title,
        cover_art=cover_art,
        feed_url=feed_url,
        matched_episode=matched_episode,
        recent_episodes=episodes,
    )

    result_dict = result.model_dump()
    _set_cached(cache_key, result_dict)
    return result_dict


@app.get("/api/episode-by-title")
async def find_episode_by_title(
    url: str = Query(..., description="Apple Podcasts 節目連結（不需含單集 id）"),
    title_contains: str = Query(..., description="單集標題包含的關鍵字，用來在 RSS 裡比對正確的那一集"),
    country: str = Query("tw"),
):
    """
    輔助 endpoint：當 i= 的 episode id 跟 RSS guid 對不上時，
    讓使用者用「單集標題關鍵字」直接從 RSS 裡撈出正確那一集。
    """
    try:
        podcast_id, _ = parse_apple_podcast_url(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    feed_url, podcast_title, cover_art = await fetch_itunes_feed_url(podcast_id, country)

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(feed_url, headers={"User-Agent": "BilingoPodcastApp/0.1"})
        resp.raise_for_status()

    parsed_feed = feedparser.parse(resp.content)
    needle = title_contains.strip().lower()

    for entry in parsed_feed.entries:
        if needle in getattr(entry, "title", "").lower():
            ep = _entry_to_episode(entry)
            if ep:
                if not ep.cover_art:
                    ep.cover_art = cover_art
                return {"podcast_title": podcast_title, "episode": ep.model_dump()}

    raise HTTPException(status_code=404, detail="在這個節目的 RSS Feed 裡找不到標題符合的單集")


# --------------------------------------------------------------------------
# 自動逐字稿來源：YouTube 字幕 + 免費翻譯
# --------------------------------------------------------------------------

class TranscriptLineOut(BaseModel):
    t: float
    en: str
    zh: str = ""


class TranscriptOut(BaseModel):
    source: str
    lines: List[TranscriptLineOut]
    translation_warning: Optional[str] = None


@app.get("/api/transcript/youtube", response_model=TranscriptOut)
async def get_youtube_transcript(
    video: str = Query(..., description="YouTube 影片連結或 11 字元的影片 ID"),
    translate: bool = Query(True, description="是否同時翻譯成中文（用免費、非官方的翻譯服務）"),
):
    """
    抓取 YouTube 影片的英文字幕，並（可選）翻譯成中文。

    限制：
      - 需要該影片本身有字幕（人工上傳或自動生成皆可）
      - 翻譯使用免費、非官方的 googletrans，不保證 100% 成功率；
        若翻譯失敗，該句的 zh 會是空字串，不會讓整個請求失敗
    """
    try:
        lines = fetch_youtube_transcript(video)
    except TranscriptSourceError as e:
        raise HTTPException(status_code=502, detail=str(e))

    translation_warning = None
    if translate:
        try:
            lines = await translate_lines_to_zh(lines)
            if any(not l.zh for l in lines):
                translation_warning = (
                    "部分句子翻譯失敗（免費翻譯服務不穩定），"
                    "這些句子目前只有英文，可以之後再手動補上中文。"
                )
        except TranscriptSourceError as e:
            translation_warning = f"翻譯整體失敗，僅回傳英文字幕：{e}"

    return TranscriptOut(
        source="youtube",
        lines=[TranscriptLineOut(**l.to_dict()) for l in lines],
        translation_warning=translation_warning,
    )


# --------------------------------------------------------------------------
# 自動逐字稿來源：Substack（預留，尚未實作）
# --------------------------------------------------------------------------

@app.get("/api/transcript/substack")
async def get_substack_transcript(
    article_url: str = Query(..., description="Substack newsletter 文章網址"),
):
    """
    預留 endpoint：之後若要支援 Substack 的 Transcript 區塊自動解析。
    目前一律回傳 501，明確告知前端「尚未實作」，而不是默默回空結果。
    """
    try:
        fetch_substack_transcript(article_url)
    except TranscriptSourceError as e:
        raise HTTPException(status_code=501, detail=str(e))
