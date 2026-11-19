#!/usr/bin/env python3
"""
Daily Automated Malayalam Everyday Curiosity, Science, Tech & Fact-Check Generator
Updates:
  - Trending Topic Discovery: Queries recent high-view YouTube videos (last 7 days,
    sorted by viewCount) + Google Trends India RSS + HowStuffWorks/ScienceDaily RSS.
  - Branded Cross-Platform Outro: Mentions "Daily Logic Malayalam" and asks viewers to
    both Follow (IG/FB) and Subscribe (YouTube).
  - IG & FB Reels Cover Thumbnail + Hashtags: Passes `thumb_offset="0"` on Instagram
    Reels (using the 0.6s colorful curiosity cover frame) + platform-tailored Reels tags.
"""

import asyncio
import json
import os
import random
import re
import subprocess
import sys
import textwrap
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Tuple

import edge_tts
import requests
from google import genai
from google.genai import types
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from PIL import Image, ImageDraw, ImageEnhance, ImageFont
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GEMINI_MODEL_CANDIDATES = [
    os.getenv("GEMINI_MODEL", "").strip(),
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-2.5-flash",
]

YOUTUBE_TOPIC_QUERIES = [
    "Malayalam interesting facts trending",
    "Malayalam science technology gadgets facts",
    "Malayalam car bike electronics how it works",
    "how things work everyday science viral",
    "amazing facts daily life earth nature",
]

RSS_FEEDS = [
    "https://trends.google.com/trending/rss?geo=IN",
    "https://syndication.howstuffworks.com/rss/HSW",
    "https://www.sciencedaily.com/rss/strange_offbeat.xml",
    "https://www.sciencedaily.com/rss/health_medicine.xml",
]

EVERGREEN_DAILY_TOPICS = [
    "Why Onions Make You Cry and the Simple Kitchen Trick to Stop It",
    "Fact Check: Does Fast Charging Actually Ruin Your Smartphone Battery?",
    "Why Do Mosquitoes Bite Certain People More Than Others?",
    "How a Car ABS Braking System Prevents Skidding on Wet Roads",
    "Why Yawning Is Contagious: The Strange Brain Science Behind It",
    "How Pressure Cookers Cook Food in Half the Time",
    "Why Airplane Windows Have a Tiny Hole at the Bottom",
    "Fact Check: Does Cracking Your Knuckles Really Cause Arthritis?",
    "How Noise-Canceling Headphones Create Silence Using Sound Waves",
    "Why Honey Never Spoils Even After Thousands of Years",
    "How an Inverter Powers Your Home During a Power Cut",
    "Why Cats Always Land on Their Feet: Nature's Built-in Gyroscope",
]

# Branded outro covering YouTube (Subscribe) + Instagram/Facebook (Follow)
MANDATORY_OUTRO_ML = (
    "ഇത്തരം കൗതുകകരവും ഇൻട്രെസ്റ്റിംഗും ആയിട്ടുള്ള കണ്ടെന്റുകൾ കൂടുതൽ കാണുന്നതിന് "
    "ഡെയിലി ലോജിക് മലയാളം എന്ന നമ്മുടെ പേജും ചാനലും ഫോളോ ചെയ്യാനും സബ്സ്ക്രൈബ് ചെയ്യാനും മറക്കല്ലേ."
)

WATERMARK_TEXT = "Daily Logic Malayalam"
HISTORY_FILE = Path("used_topics.json")
WORK_DIR = Path("build_short")
META_CACHE_FILE = WORK_DIR / "meta_payload.json"
VOICE_NAME = os.getenv("TTS_VOICE", "ml-IN-MidhunNeural")
PRIVACY_STATUS = os.getenv("YT_PRIVACY_STATUS", "private")
SKIP_UPLOAD = os.getenv("SKIP_UPLOAD", "false").lower() == "true"
CATEGORY_ID = os.getenv("YT_CATEGORY_ID", "28")
GRAPH_API_VERSION = "v21.0"


# ---------------------------------------------------------------------------
# Structured Output Schema for Gemini
# ---------------------------------------------------------------------------
class ShortScriptPlan(BaseModel):
    title: str = Field(
        description="Catchy Malayalam + English YouTube Shorts title under 70 characters, including #Shorts #Malayalam."
    )
    hook_headline_ml: str = Field(
        description="Short 4-to-7 word Malayalam headline shown at the top of the video frame."
    )
    thumbnail_question_ml: str = Field(
        description="Punchy 3-to-5 word curiosity-sparking Malayalam question for the thumbnail (ending with '?' or '!')."
    )
    description: str = Field(
        description="Engaging Malayalam & English YouTube description explaining the fact or curiosity, plus 5-8 YouTube hashtags."
    )
    reels_caption_ml: str = Field(
        description="Engaging Instagram & Facebook Reels caption in Malayalam + English with 10-12 trending hashtags including #DailyLogicMalayalam #MalayalamReels #KeralaReels #MalayalamFacts #ScienceMalayalam."
    )
    tags: List[str] = Field(
        description="6 to 10 relevant SEO keywords in Malayalam and English without # symbols."
    )
    narration_malayalam: str = Field(
        description="Natural, conversational spoken Kerala Malayalam script (115 to 145 words for ~60 seconds). Use short sentences and commas for natural human pauses. Spoken style only—no textbook stiffness, no stage directions, no markdown."
    )
    search_queries: List[str] = Field(
        description="Exactly 4 concise 1-to-2 word ENGLISH visual search terms for stock videos matching the story."
    )


# ---------------------------------------------------------------------------
# YouTube OAuth Credentials Helper
# ---------------------------------------------------------------------------
def get_youtube_service():
    for var in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"):
        if not os.getenv(var, "").strip():
            raise RuntimeError(
                f"Missing required GitHub Secret '{var}'. Add it in Settings -> Secrets and variables -> Actions."
            )
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"].strip(),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"].strip(),
        client_secret=os.environ["YT_CLIENT_SECRET"].strip(),
        scopes=[
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube.readonly",
        ],
    )
    return build("youtube", "v3", credentials=creds)


# ---------------------------------------------------------------------------
# Step 1: Discover Trending Topics (Recent High-View YouTube + Google Trends + RSS)
# ---------------------------------------------------------------------------
def load_used_topics() -> List[str]:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
    return []


def save_used_topic(topic_id: str, used: List[str]) -> None:
    used.append(topic_id)
    HISTORY_FILE.write_text(json.dumps(used[-500:], indent=2), encoding="utf-8")


def fetch_daily_topic() -> dict:
    used_topics = load_used_topics()

    # 1. Search recent (last 7 days) high-view YouTube videos for trending topics
    if os.getenv("YT_REFRESH_TOKEN", "").strip() and os.getenv("YT_CLIENT_ID", "").strip():
        try:
            youtube = get_youtube_service()
            query = random.choice(YOUTUBE_TOPIC_QUERIES)
            published_after = (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            res = (
                youtube.search()
                .list(
                    part="snippet",
                    q=query,
                    type="video",
                    order="viewCount",
                    publishedAfter=published_after,
                    maxResults=15,
                )
                .execute()
            )
            for item in res.get("items", []):
                title = item["snippet"]["title"].strip()
                video_id = item["id"].get("videoId", "")
                if title and title not in used_topics:
                    return {
                        "title": title,
                        "link": f"https://www.youtube.com/watch?v={video_id}",
                        "used_list": used_topics,
                    }
        except Exception as exc:
            print(f"[WARN] YouTube trending search skipped/failed (using Trends/RSS): {exc}")

    # 2. Fallback to Google Trends India & Everyday Science RSS Feeds
    headers = {"User-Agent": "Mozilla/5.0 (DailyLogicMalayalamBot/1.0)"}
    for feed_url in RSS_FEEDS:
        try:
            resp = requests.get(feed_url, headers=headers, timeout=15)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
            for item in items:
                title_el = item.find("title") or item.find("{http://www.w3.org/2005/Atom}title")
                link_el = item.find("link") or item.find("{http://www.w3.org/2005/Atom}link")
                title = (title_el.text or "").strip() if title_el is not None else ""
                link = (link_el.text or link_el.attrib.get("href", "")).strip() if link_el is not None else ""
                if title and title not in used_topics:
                    return {"title": title, "link": link, "used_list": used_topics}
        except Exception as exc:
            print(f"[WARN] Failed reading feed {feed_url}: {exc}")

    # 3. Fallback to Evergreen bank
    for topic in EVERGREEN_DAILY_TOPICS:
        if topic not in used_topics:
            return {"title": topic, "link": "", "used_list": used_topics}

    fallback = f"Everyday Curiosity & Fact Check ({datetime.now(timezone.utc).strftime('%Y-%m-%d')})"
    return {"title": fallback, "link": "", "used_list": used_topics}


# ---------------------------------------------------------------------------
# Step 2: Generate Unique, Natural Conversational Malayalam Script
# ---------------------------------------------------------------------------
def generate_script_plan(topic_title: str, topic_link: str) -> ShortScriptPlan:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "Missing required GitHub Secret 'GEMINI_API_KEY'. Add it in Settings -> Secrets and variables -> Actions."
        )
    client = genai.Client(api_key=api_key)

    prompt = f"""
You are the host of "Daily Logic Malayalam", a warm, curious Kerala storyteller explaining fascinating everyday facts—ranging from daily-use items, food, human body, nature, animals, vehicles, electronics, gadgets, and trending curiosities to common myths.
Trending Topic Inspiration: {topic_title}
Reference: {topic_link}

CRITICAL RULES FOR UNIQUENESS & HUMAN TONE:
1. NEVER copy anyone else's script. Take ONLY the core curiosity and write a 100% original, engaging Malayalam story with relatable daily-life examples.
2. SOUND LIKE A REAL PERSON TALKING, NOT A ROBOT OR TEXTBOOK:
   - Write in warm, natural spoken Kerala Malayalam (e.g., "നമ്മൾ എപ്പോഴെങ്കിലും ആലോചിച്ചിട്ടുണ്ടോ?", "ശരിക്കും ഇതിനു പിന്നിൽ ഒരു രസകരമായ കാര്യമുണ്ട്...", "പലരും വിചാരിക്കുന്നത്...").
   - Avoid stiff bookish words like "ചെയ്യപ്പെടുന്നു", "ആകുന്നു", "പ്രവർത്തിക്കപ്പെടുന്നു".
   - Use short sentences with commas (,) so the voiceover naturally pauses and breathes like a human.
3. Focus on relatable Earth & daily-life topics (NO outer space or galaxies).
4. Target ~60 seconds (~115 to 145 Malayalam words; up to 2 minutes max if needed).
5. End naturally before the follow/subscribe line (we automatically append the branded "Daily Logic Malayalam" follow & subscribe outro).
6. Keep 'narration_malayalam' strictly to spoken Malayalam script (no [Music], no speaker labels, no asterisks, no English letters).
7. Provide 'thumbnail_question_ml': a short 3-to-5 word curiosity-sparking Malayalam question that makes viewers want to click immediately.
8. Provide 'reels_caption_ml': a punchy Malayalam/English caption for Instagram & Facebook Reels with 10-12 trending hashtags (#DailyLogicMalayalam #MalayalamReels #KeralaReels #MalayalamFacts #FactCheckMalayalam).
9. Provide 'search_queries' in ENGLISH (4 simple 1-to-2 word visual terms matching each part of the story).
"""

    last_err = None
    for model_name in [m for m in GEMINI_MODEL_CANDIDATES if m]:
        try:
            print(f"      Trying Gemini model: {model_name}")
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ShortScriptPlan,
                    temperature=0.85,
                ),
            )
            plan = ShortScriptPlan.model_validate_json(response.text)
            narration = plan.narration_malayalam.strip()
            if "ഡെയിലി ലോജിക് മലയാളം" not in narration:
                narration = f"{narration} {MANDATORY_OUTRO_ML}"
            plan.narration_malayalam = narration
            return plan
        except Exception as exc:
            print(f"[WARN] Model '{model_name}' failed: {exc}")
            last_err = exc

    raise RuntimeError(f"All Gemini models failed. Last error: {last_err}")


# ---------------------------------------------------------------------------
# Step 3: Voiceover
# ---------------------------------------------------------------------------
def get_media_duration(media_path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(media_path),
    ]
    return float(subprocess.check_output(cmd, text=True).strip())


def format_srt_ts(seconds: float) -> str:
    ms = int(round((seconds - int(seconds)) * 1000))
    total_s = int(seconds)
    h = total_s // 3600
    m = (total_s % 3600) // 60
    s = total_s % 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_estimated_srt(narration: str, audio_duration: float, srt_path: Path) -> None:
    sentences = [s.strip() for s in re.split(r"[.!?।]+", narration) if s.strip()]
    if not sentences:
        sentences = [narration]
    total_chars = sum(len(s) for s in sentences) or 1
    cursor = 0.0
    srt_lines = []
    for idx, sent in enumerate(sentences, start=1):
        dur = max(2.0, audio_duration * (len(sent) / total_chars))
        end = min(audio_duration, cursor + dur)
        srt_lines.append(f"{idx}\n{format_srt_ts(cursor)} --> {format_srt_ts(end)}\n{sent}\n")
        cursor = end
    srt_path.write_text("\n".join(srt_lines), encoding="utf-8")


async def create_edge_tts_voiceover(narration: str, audio_path: Path, srt_path: Path) -> None:
    communicate = edge_tts.Communicate(narration, VOICE_NAME, rate="-4%", pitch="-2Hz")
    submaker = edge_tts.SubMaker()
    with open(audio_path, "wb") as audio_file:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_file.write(chunk["data"])
            elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                submaker.feed(chunk)
    srt_path.write_text(submaker.get_srt(), encoding="utf-8")


def create_voiceover_and_srt(narration: str, audio_path: Path, srt_path: Path) -> None:
    el_api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    el_voice_id = os.getenv("ELEVENLABS_VOICE_ID", "").strip()

    if el_api_key and el_voice_id:
        print(f"      Using YOUR cloned ElevenLabs voice ({el_voice_id})...")
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{el_voice_id}"
        resp = requests.post(
            url,
            headers={"xi-api-key": el_api_key, "Content-Type": "application/json"},
            json={
                "text": narration,
                "model_id": os.getenv("ELEVENLABS_MODEL_ID", "eleven_v3"),
                "voice_settings": {"stability": 0.45, "similarity_boost": 0.8},
            },
            timeout=90,
        )
        resp.raise_for_status()
        audio_path.write_bytes(resp.content)
        dur = get_media_duration(audio_path)
        build_estimated_srt(narration, dur, srt_path)
    else:
        print(f"      Using free Edge Neural voice ({VOICE_NAME}, rate=-4%, pitch=-2Hz)...")
        asyncio.run(create_edge_tts_voiceover(narration, audio_path, srt_path))


# ---------------------------------------------------------------------------
# Step 4: Download Stock Clips
# ---------------------------------------------------------------------------
def download_stock_clips(queries: List[str], out_dir: Path) -> List[Path]:
    pexels_key = os.getenv("PEXELS_API_KEY", "").strip()
    pixabay_key = os.getenv("PIXABAY_API_KEY", "").strip()
    downloaded_clips: List[Path] = []

    if pexels_key:
        headers = {"Authorization": pexels_key}
        for idx, query in enumerate(queries):
            encoded_q = urllib.parse.quote(query)
            url = f"https://api.pexels.com/videos/search?query={encoded_q}&per_page=5"
            try:
                resp = requests.get(url, headers=headers, timeout=20)
                resp.raise_for_status()
                videos = resp.json().get("videos", [])
                if videos:
                    clip_url = videos[0]["video_files"][0]["link"]
                    clip_path = out_dir / f"raw_clip_{idx}.mp4"
                    with requests.get(clip_url, stream=True, timeout=60) as r:
                        r.raise_for_status()
                        with open(clip_path, "wb") as f:
                            for chunk in r.iter_content(chunk_size=8192):
                                f.write(chunk)
                    downloaded_clips.append(clip_path)
            except Exception as exc:
                print(f"[WARN] Pexels fetch failed for '{query}': {exc}")

    if not downloaded_clips and pixabay_key:
        for idx, query in enumerate(queries):
            encoded_q = urllib.parse.quote(query)
            url = f"https://pixabay.com/api/videos/?key={pixabay_key}&q={encoded_q}&per_page=5"
            try:
                resp = requests.get(url, timeout=20)
                resp.raise_for_status()
                hits = resp.json().get("hits", [])
                if not hits:
                    fallback_url = f"https://pixabay.com/api/videos/?key={pixabay_key}&q=daily+life+nature&per_page=5"
                    hits = requests.get(fallback_url, timeout=20).json().get("hits", [])

                if hits:
                    videos_dict = hits[0].get("videos", {})
                    clip_url = (
                        videos_dict.get("medium", {}).get("url")
                        or videos_dict.get("small", {}).get("url")
                        or videos_dict.get("large", {}).get("url")
                    )
                    if clip_url:
                        clip_path = out_dir / f"raw_clip_{idx}.mp4"
                        with requests.get(clip_url, stream=True, timeout=60) as r:
                            r.raise_for_status()
                            with open(clip_path, "wb") as f:
                                for chunk in r.iter_content(chunk_size=8192):
                                    f.write(chunk)
                        downloaded_clips.append(clip_path)
            except Exception as exc:
                print(f"[WARN] Pixabay fetch failed for '{query}': {exc}")

    if not downloaded_clips:
        print("[INFO] Using clean animated gradient backgrounds via FFmpeg...")
        colors = ["#132238", "#1B3B5F", "#0F2C4C", "#162A45"]
        for idx, color in enumerate(colors):
            clip_path = out_dir / f"raw_clip_{idx}.mp4"
            cmd = [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=1080x1920:r=30:d=15",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(clip_path),
            ]
            subprocess.run(cmd, check=True)
            downloaded_clips.append(clip_path)

    return downloaded_clips


# ---------------------------------------------------------------------------
# Step 5: Thumbnail & Fast Video Assembly
# ---------------------------------------------------------------------------
def find_malayalam_font() -> str:
    candidates = [
        "/usr/share/fonts/truetype/noto/NotoSansMalayalam-Bold.ttf",
        "/usr/share/fonts/truetype/malayalam/Manjari-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansMalayalam-Regular.ttf",
        "/usr/share/fonts/truetype/smc/Manjari-Bold.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    return "DejaVuSans-Bold.ttf"


def generate_curiosity_thumbnail(
    lead_clip: Path, question_ml: str, out_jpg: Path, out_vertical_png: Path, font_path: str
) -> None:
    raw_frame = out_jpg.parent / "thumb_raw_frame.jpg"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            "00:00:01.0",
            "-i",
            str(lead_clip),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(raw_frame),
        ],
        check=True,
    )

    base = Image.open(raw_frame).convert("RGBA")
    base = ImageEnhance.Color(base).enhance(1.35)
    base = ImageEnhance.Contrast(base).enhance(1.15)

    def build_card(target_w: int, target_h: int, is_vertical: bool) -> Image.Image:
        src_w, src_h = base.size
        scale = max(target_w / src_w, target_h / src_h)
        resized = base.resize((int(src_w * scale), int(src_h * scale)), Image.Resampling.LANCZOS)
        left = (resized.width - target_w) // 2
        top = (resized.height - target_h) // 2
        canvas = resized.crop((left, top, left + target_w, top + target_h))

        overlay = Image.new("RGBA", (target_w, target_h), (10, 16, 32, 95))
        draw_ov = ImageDraw.Draw(overlay)
        band_y0 = int(target_h * (0.48 if is_vertical else 0.36))
        draw_ov.rectangle([0, band_y0, target_w, target_h], fill=(10, 16, 34, 195))
        canvas = Image.alpha_composite(canvas, overlay)

        draw = ImageDraw.Draw(canvas)
        border_w = 18 if is_vertical else 14
        draw.rectangle(
            [border_w // 2, border_w // 2, target_w - border_w // 2, target_h - border_w // 2],
            outline=(255, 214, 0, 255),
            width=border_w,
        )

        badge_font = ImageFont.truetype(font_path, 42 if is_vertical else 34)
        badge_text = "FACT CHECK  |  സത്യം എന്ത്?"
        bx0 = int(target_w * 0.14)
        bx1 = int(target_w * 0.86)
        by0 = int(target_h * (0.12 if is_vertical else 0.09))
        by1 = by0 + (88 if is_vertical else 72)
        draw.rounded_rectangle([bx0, by0, bx1, by1], radius=44, fill=(255, 61, 46, 255), outline=(255, 255, 255, 255), width=4)
        draw.text((target_w // 2, (by0 + by1) // 2), badge_text, font=badge_font, fill=(255, 255, 255, 255), anchor="mm")

        q_font = ImageFont.truetype(font_path, 76 if is_vertical else 66)
        wrapped_q = textwrap.fill(question_ml, width=16 if is_vertical else 22)
        q_y = int(target_h * (0.68 if is_vertical else 0.62))
        draw.multiline_text(
            (target_w // 2 + 4, q_y + 4),
            wrapped_q,
            font=q_font,
            fill=(0, 0, 0, 230),
            anchor="mm",
            spacing=22,
            align="center",
        )
        draw.multiline_text(
            (target_w // 2, q_y),
            wrapped_q,
            font=q_font,
            fill=(255, 235, 59, 255),
            anchor="mm",
            spacing=22,
            align="center",
        )

        brand_font = ImageFont.truetype(font_path, 34 if is_vertical else 28)
        draw.text(
            (target_w // 2, int(target_h * 0.90)),
            "DAILY LOGIC MALAYALAM",
            font=brand_font,
            fill=(0, 229, 255, 255),
            anchor="mm",
        )
        return canvas.convert("RGB")

    build_card(1280, 720, False).save(out_jpg, quality=92)
    build_card(1080, 1920, True).save(out_vertical_png)


def parse_srt_time(ts: str) -> float:
    h, m, s_ms = ts.strip().split(":")
    s, ms = s_ms.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_and_coalesce_srt(srt_path: Path, max_words_per_cue: int = 8) -> List[Tuple[float, float, str]]:
    if not srt_path.exists():
        return []
    raw = srt_path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    blocks = re.split(r"\r?\n\r?\n", raw)
    raw_cues: List[Tuple[float, float, str]] = []
    for block in blocks:
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if len(lines) >= 3 and "-->" in lines[1]:
            start_s, end_s = lines[1].split("-->")
            text = " ".join(lines[2:])
            raw_cues.append((parse_srt_time(start_s), parse_srt_time(end_s), text))

    if len(raw_cues) <= 20:
        return raw_cues

    merged: List[Tuple[float, float, str]] = []
    buf_words: List[str] = []
    buf_start = raw_cues[0][0]
    buf_end = raw_cues[0][1]

    for start_t, end_t, text in raw_cues:
        words = text.split()
        if len(buf_words) + len(words) > max_words_per_cue or (end_t - buf_start) > 4.5:
            if buf_words:
                merged.append((buf_start, buf_end, " ".join(buf_words)))
            buf_words = words
            buf_start = start_t
            buf_end = end_t
        else:
            buf_words.extend(words)
            buf_end = end_t

    if buf_words:
        merged.append((buf_start, buf_end, " ".join(buf_words)))
    return merged


def render_malayalam_header_png(headline: str, out_png: Path, font_path: str) -> None:
    img = Image.new("RGBA", (1080, 260), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(font_path, 48)
    wrapped = textwrap.fill(headline, width=26)

    draw.rounded_rectangle(
        [70, 30, 1010, 230],
        radius=28,
        fill=(19, 34, 56, 220),
        outline=(0, 229, 255, 220),
        width=3,
    )
    draw.multiline_text(
        (540, 130),
        wrapped,
        font=font,
        fill=(255, 255, 255, 255),
        anchor="mm",
        spacing=12,
        align="center",
    )
    img.save(out_png)


def render_malayalam_subtitle_png(text: str, out_png: Path, font_path: str) -> None:
    img = Image.new("RGBA", (1080, 360), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(font_path, 46)

    wrapped = textwrap.fill(text, width=28)
    bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=14, align="center")
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    pad_x, pad_y = 36, 22
    box_w = tw + pad_x * 2
    box_h = th + pad_y * 2
    x0 = (1080 - box_w) // 2
    y0 = (360 - box_h) // 2

    draw.rounded_rectangle(
        [x0, y0, x0 + box_w, y0 + box_h],
        radius=24,
        fill=(12, 18, 34, 215),
        outline=(0, 229, 255, 180),
        width=2,
    )
    draw.multiline_text(
        (540, 180),
        wrapped,
        font=font,
        fill=(255, 235, 59, 255),
        anchor="mm",
        spacing=14,
        align="center",
    )
    img.save(out_png)


def assemble_short_video(
    clips: List[Path],
    audio_path: Path,
    srt_path: Path,
    hook_headline: str,
    thumb_vertical_png: Path,
    final_video_path: Path,
    work_dir: Path,
) -> None:
    audio_duration = get_media_duration(audio_path)
    segment_duration = max(3.5, (audio_duration + 0.5) / len(clips))

    normalized_clips: List[Path] = []
    for idx, clip in enumerate(clips):
        norm_path = work_dir / f"norm_{idx}.mp4"
        vf_complex = (
            "[0:v]split=2[bg][fg];"
            "[bg]scale=135:240:force_original_aspect_ratio=increase,"
            "crop=135:240,boxblur=4:1,scale=1080:1920,eq=brightness=-0.15[bg_blur];"
            "[fg]scale=1080:1240:force_original_aspect_ratio=decrease[fg_fit];"
            "[bg_blur][fg_fit]overlay=(W-w)/2:(H-h)/2-40,fps=30,format=yuv420p[out]"
        )
        cmd = [
            "ffmpeg",
            "-y",
            "-stream_loop",
            "-1",
            "-i",
            str(clip),
            "-t",
            f"{segment_duration:.2f}",
            "-filter_complex",
            vf_complex,
            "-map",
            "[out]",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            str(norm_path),
        ]
        subprocess.run(cmd, check=True)
        normalized_clips.append(norm_path)

    concat_list_path = work_dir / "concat_list.txt"
    concat_lines = [f"file '{c.resolve()}'" for c in normalized_clips]
    concat_list_path.write_text("\n".join(concat_lines), encoding="utf-8")

    merged_bg_path = work_dir / "merged_bg.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list_path),
            "-c",
            "copy",
            str(merged_bg_path),
        ],
        check=True,
    )

    cues = parse_and_coalesce_srt(srt_path, max_words_per_cue=8)
    print(f"      Rendering {len(cues)} coalesced Malayalam subtitle cues...")
    font_path = find_malayalam_font()

    header_png = work_dir / "header.png"
    render_malayalam_header_png(hook_headline, header_png, font_path)

    cmd_inputs = [
        "-i", str(merged_bg_path),
        "-i", str(audio_path),
        "-i", str(header_png),
        "-i", str(thumb_vertical_png),
    ]
    filter_parts = [
        "[0:v][2:v]overlay=0:120[vhead]",
        "[vhead][3:v]overlay=0:0:enable='between(t,0,0.6)'[vhook]",
    ]
    last_label = "[vhook]"

    for idx, (start_t, end_t, text) in enumerate(cues):
        png_path = work_dir / f"sub_{idx}.png"
        render_malayalam_subtitle_png(text, png_path, font_path)
        cmd_inputs.extend(["-i", str(png_path)])
        input_idx = idx + 4
        out_label = f"[v{idx}]"
        filter_parts.append(
            f"{last_label}[{input_idx}:v]overlay=0:1390:enable='between(t,{start_t:.2f},{end_t:.2f})'{out_label}"
        )
        last_label = out_label

    wm_x = (
        "if(eq(mod(floor(t/8)\\,4)\\,0)\\,48\\,"
        "if(eq(mod(floor(t/8)\\,4)\\,1)\\,w-tw-48\\,"
        "if(eq(mod(floor(t/8)\\,4)\\,2)\\,w-tw-48\\,48)))"
    )
    wm_y = (
        "if(eq(mod(floor(t/8)\\,4)\\,0)\\,40\\,"
        "if(eq(mod(floor(t/8)\\,4)\\,1)\\,40\\,"
        "if(eq(mod(floor(t/8)\\,4)\\,2)\\,h-th-480\\,h-th-480)))"
    )
    filter_parts.append(
        f"{last_label}drawtext=text='{WATERMARK_TEXT}':fontsize=32:"
        "fontcolor=white@0.5:shadowcolor=black@0.25:shadowx=2:shadowy=2:"
        f"x='{wm_x}':y='{wm_y}'[vout]"
    )

    cmd_final = [
        "ffmpeg",
        "-y",
        *cmd_inputs,
        "-filter_complex",
        ";".join(filter_parts),
        "-map",
        "[vout]",
        "-map",
        "1:a:0",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-ar",
        "44100",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        "-shortest",
        str(final_video_path),
    ]
    subprocess.run(cmd_final, check=True)


# ---------------------------------------------------------------------------
# Step 6A: Upload Video + Custom Thumbnail to YouTube
# ---------------------------------------------------------------------------
def upload_to_youtube(video_path: Path, thumb_jpg: Path, plan: ShortScriptPlan) -> str:
    youtube = get_youtube_service()

    body = {
        "snippet": {
            "title": plan.title[:100],
            "description": plan.description,
            "tags": plan.tags,
            "categoryId": CATEGORY_ID,
            "defaultLanguage": "ml",
            "defaultAudioLanguage": "ml",
        },
        "status": {
            "privacyStatus": PRIVACY_STATUS,
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": False,
        },
    }

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        chunksize=1024 * 1024 * 5,
        resumable=True,
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"[INFO] YouTube upload progress: {int(status.progress() * 100)}%")

    video_id = response["id"]
    print(f"[SUCCESS] Uploaded YouTube Video ({PRIVACY_STATUS})! URL: https://youtube.com/shorts/{video_id}")

    try:
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(str(thumb_jpg), mimetype="image/jpeg"),
        ).execute()
        print("[SUCCESS] Custom curiosity thumbnail uploaded to YouTube!")
    except Exception as exc:
        print(f"[INFO] Custom thumbnail API skipped (Shorts uses the 0.6s opening hook frame): {exc}")

    return video_id


# ---------------------------------------------------------------------------
# Step 6B & 6C: Meta Reels Cross-Posting (Instagram Cover + Hashtags & FB Scheduled)
# ---------------------------------------------------------------------------
def upload_to_instagram_reels(video_path: Path, caption: str) -> None:
    access_token = os.getenv("META_ACCESS_TOKEN", "").strip()
    ig_user_id = os.getenv("IG_USER_ID", "").strip()
    if not access_token or not ig_user_id:
        print("[SKIP] Instagram Reels skipped (META_ACCESS_TOKEN or IG_USER_ID not set).")
        return

    video_bytes = video_path.read_bytes()
    file_size = len(video_bytes)
    init_url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{ig_user_id}/media"

    container_id = None
    for attempt in range(1, 3):
        # thumb_offset="0" uses the colorful 0.6s curiosity thumbnail frame at the start of the video
        init_resp = requests.post(
            init_url,
            data={
                "media_type": "REELS",
                "upload_type": "resumable",
                "caption": caption,
                "thumb_offset": "0",
                "share_to_feed": "true",
                "access_token": access_token,
            },
            timeout=30,
        )
        if not init_resp.ok:
            print(f"[ERROR] Instagram init HTTP {init_resp.status_code}: {init_resp.text}")
            init_resp.raise_for_status()
        container_data = init_resp.json()
        container_id = container_data["id"]
        upload_uri = container_data.get(
            "uri", f"https://rupload.facebook.com/ig-api-upload/{GRAPH_API_VERSION}/{container_id}"
        )

        rupload_resp = requests.post(
            upload_uri,
            headers={
                "Authorization": f"OAuth {access_token}",
                "offset": "0",
                "file_size": str(file_size),
                "Content-Length": str(file_size),
                "Content-Type": "application/octet-stream",
            },
            data=video_bytes,
            timeout=180,
        )
        if rupload_resp.ok:
            break
        print(
            f"[WARN] Instagram rupload attempt {attempt} failed "
            f"(HTTP {rupload_resp.status_code}): {rupload_resp.text}"
        )
        if attempt == 2:
            rupload_resp.raise_for_status()
        time.sleep(5)

    status_url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{container_id}"
    for _ in range(20):
        time.sleep(10)
        stat_resp = requests.get(
            status_url,
            params={"fields": "status_code,status", "access_token": access_token},
            timeout=20,
        ).json()
        code = stat_resp.get("status_code")
        print(f"[INFO] Instagram container status: {code}")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram Reels processing failed: {stat_resp}")

    pub_url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{ig_user_id}/media_publish"
    pub_resp = requests.post(
        pub_url,
        data={"creation_id": container_id, "access_token": access_token},
        timeout=30,
    )
    pub_resp.raise_for_status()
    print(f"[SUCCESS] Published Instagram Reel with cover thumbnail & tags! Media ID: {pub_resp.json().get('id')}")


def upload_to_facebook_reels(
    video_path: Path, title: str, description: str, delay_minutes: int = 120
) -> None:
    access_token = os.getenv("META_ACCESS_TOKEN", "").strip()
    fb_page_id = os.getenv("FB_PAGE_ID", "").strip()
    if not access_token or not fb_page_id:
        print("[SKIP] Facebook Reels skipped (META_ACCESS_TOKEN or FB_PAGE_ID not set).")
        return

    video_bytes = video_path.read_bytes()
    file_size = len(video_bytes)
    start_url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{fb_page_id}/video_reels"
    start_resp = requests.post(
        start_url,
        data={"upload_phase": "start", "access_token": access_token},
        timeout=30,
    )
    if not start_resp.ok:
        print(f"[ERROR] Facebook start HTTP {start_resp.status_code}: {start_resp.text}")
        start_resp.raise_for_status()
    start_data = start_resp.json()
    video_id = start_data["video_id"]
    upload_url = start_data.get(
        "upload_url", f"https://rupload.facebook.com/video-upload/{GRAPH_API_VERSION}/{video_id}"
    )

    up_resp = requests.post(
        upload_url,
        headers={
            "Authorization": f"OAuth {access_token}",
            "offset": "0",
            "file_size": str(file_size),
            "Content-Length": str(file_size),
            "Content-Type": "application/octet-stream",
        },
        data=video_bytes,
        timeout=180,
    )
    if not up_resp.ok:
        print(f"[ERROR] Facebook upload HTTP {up_resp.status_code}: {up_resp.text}")
        up_resp.raise_for_status()

    fb_state = os.getenv("FB_VIDEO_STATE", "PUBLISHED").upper()
    finish_payload = {
        "upload_phase": "finish",
        "video_id": video_id,
        "video_state": fb_state,
        "description": description,
        "title": title[:100],
        "access_token": access_token,
    }
    if fb_state == "SCHEDULED":
        publish_ts = int((datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)).timestamp())
        finish_payload["scheduled_publish_time"] = str(publish_ts)

    finish_resp = requests.post(start_url, data=finish_payload, timeout=30)
    if not finish_resp.ok:
        print(f"[ERROR] Facebook finish HTTP {finish_resp.status_code}: {finish_resp.text}")
        finish_resp.raise_for_status()
    print(f"[SUCCESS] Facebook Page Reel ({fb_state}) with tags! Video ID: {video_id}")


# ---------------------------------------------------------------------------
# Helper: One-Time Local OAuth Refresh Token Generator for YouTube
# ---------------------------------------------------------------------------
def generate_refresh_token_interactive() -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    secret_file = Path("client_secret.json")
    if not secret_file.exists():
        sys.exit("Error: Place your downloaded 'client_secret.json' in the current directory first.")

    flow = InstalledAppFlow.from_client_secrets_file(
        str(secret_file),
        scopes=[
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube.readonly",
        ],
    )
    creds = flow.run_local_server(port=8080, access_type="offline", prompt="consent")
    print("\n=== Save these 3 values in your GitHub Repository Secrets ===")
    print(f"YT_CLIENT_ID:     {creds.client_id}")
    print(f"YT_CLIENT_SECRET: {creds.client_secret}")
    print(f"YT_REFRESH_TOKEN: {creds.refresh_token}\n")


# ---------------------------------------------------------------------------
# Main Pipeline Execution
# ---------------------------------------------------------------------------
def main() -> None:
    if "--auth" in sys.argv:
        generate_refresh_token_interactive()
        return

    if "--publish-ig-only" in sys.argv:
        final_video_path = WORK_DIR / "final_short.mp4"
        if not final_video_path.exists() or not META_CACHE_FILE.exists():
            print("[SKIP] No cached video found for 7:00 PM Instagram Reels publish.")
            return
        meta = json.loads(META_CACHE_FILE.read_text(encoding="utf-8"))
        upload_to_instagram_reels(final_video_path, meta["reels_caption"])
        return

    if "--publish-fb-only" in sys.argv:
        final_video_path = WORK_DIR / "final_short.mp4"
        if not final_video_path.exists() or not META_CACHE_FILE.exists():
            print("[SKIP] No cached video found for 8:00 PM Facebook Reels publish.")
            return
        meta = json.loads(META_CACHE_FILE.read_text(encoding="utf-8"))
        upload_to_facebook_reels(final_video_path, meta["title"], meta["reels_caption"])
        return

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    audio_path = WORK_DIR / "voiceover.mp3"
    srt_path = WORK_DIR / "captions.srt"
    thumb_jpg = WORK_DIR / "thumbnail.jpg"
    thumb_vertical_png = WORK_DIR / "thumb_vertical.png"
    final_video_path = WORK_DIR / "final_short.mp4"

    print("[1/7] Finding a trending Everyday Curiosity / Fact-Check topic...")
    topic_info = fetch_daily_topic()
    print(f"      Selected topic: {topic_info['title']}")

    print("[2/7] Writing unique, human-tone Malayalam script with Gemini...")
    plan = generate_script_plan(topic_info["title"], topic_info["link"])
    print(f"      Title: {plan.title}")
    print(f"      Thumbnail Hook: {plan.thumbnail_question_ml}")

    print("[3/7] Generating voiceover & subtitles...")
    create_voiceover_and_srt(plan.narration_malayalam, audio_path, srt_path)

    print("[4/7] Downloading stock video clips...")
    clips = download_stock_clips(plan.search_queries, WORK_DIR)

    print("[5/7] Generating colorful curiosity thumbnail & rendering fast 1080x1920 video...")
    font_path = find_malayalam_font()
    generate_curiosity_thumbnail(
        clips[0], plan.thumbnail_question_ml, thumb_jpg, thumb_vertical_png, font_path
    )
    assemble_short_video(
        clips,
        audio_path,
        srt_path,
        plan.hook_headline_ml,
        thumb_vertical_png,
        final_video_path,
        WORK_DIR,
    )

    reels_caption = f"{plan.title}\n\n{plan.reels_caption_ml}"
    META_CACHE_FILE.write_text(
        json.dumps(
            {"title": plan.title, "reels_caption": reels_caption}, ensure_ascii=False
        ),
        encoding="utf-8",
    )

    if SKIP_UPLOAD:
        print("[SKIP] SKIP_UPLOAD=true — Video saved without uploading.")
        return

    print(f"[6/6] Uploading video & thumbnail to YouTube as {PRIVACY_STATUS.upper()}...")
    upload_to_youtube(final_video_path, thumb_jpg, plan)

    save_used_topic(topic_info["title"], topic_info["used_list"])
    print("[DONE] 6:00 PM build finished (YouTube Private + cached for 7 PM IG & 8 PM FB Reels).")


if __name__ == "__main__":
    main()