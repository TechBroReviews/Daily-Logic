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
import math
import struct
import time
import wave
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
        description="Punchy 3-to-5 word curiosity-sparking Malayalam dialogue/question for the thumbnail (ending with '?' or '!')."
    )
    thumbnail_image_query: str = Field(
        description="2-3 concrete English words for the exact main subject photo on Pixabay (e.g. 'denim jeans pocket', 'mosquito skin', 'car brake')."
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
    key_points_ml: List[str] = Field(
        description="Exactly 2 short, punchy Malayalam labels (3 to 5 words each) shown briefly above the 2D animation."
    )
    animation_scene: str = Field(
        description="Best matching 2D animation type from: 'wheel_brake', 'sound_waves', 'battery_heat', 'pressure_flow', 'gear_mechanism', 'light_optics', 'heart_pulse', 'comparison_bars'."
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
9. Provide 'search_queries' in ENGLISH (4 literal 2-to-3 word visual scene descriptions—NEVER use abbreviations like ABS, LED, AC, or USB; e.g., use 'car driving wet road' instead of 'ABS').
10. Provide 'key_points_ml': 2 very short Malayalam labels (3-5 words each) for the 2D animation title.
11. Pick 'animation_scene' from: 'wheel_brake', 'sound_waves', 'battery_heat', 'pressure_flow', 'gear_mechanism', 'light_optics', 'heart_pulse', 'comparison_bars'.
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
def _query_matches_tags(query: str, tags_str: str) -> bool:
    if not tags_str:
        return True
    q_words = [w.lower() for w in re.findall(r"[a-zA-Z]{3,}", query)]
    if not q_words:
        return True
    t_lower = tags_str.lower()
    return any(w in t_lower for w in q_words)


def download_stock_clips(queries: List[str], out_dir: Path) -> List[Path]:
    pexels_key = os.getenv("PEXELS_API_KEY", "").strip()
    pixabay_key = os.getenv("PIXABAY_API_KEY", "").strip()
    downloaded_clips: List[Path] = []

    for idx, query in enumerate(queries):
        clip_path = out_dir / f"raw_clip_{idx}.mp4"
        got_clip = False

        if pexels_key:
            try:
                resp = requests.get(
                    f"https://api.pexels.com/videos/search?query={urllib.parse.quote(query)}&per_page=5",
                    headers={"Authorization": pexels_key},
                    timeout=20,
                )
                resp.raise_for_status()
                for v in resp.json().get("videos", []):
                    if _query_matches_tags(query, v.get("url", "")):
                        clip_url = v["video_files"][0]["link"]
                        with requests.get(clip_url, stream=True, timeout=60) as r:
                            r.raise_for_status()
                            with open(clip_path, "wb") as f:
                                for chunk in r.iter_content(chunk_size=8192):
                                    f.write(chunk)
                        got_clip = True
                        break
            except Exception as exc:
                print(f"[WARN] Pexels fetch failed for '{query}': {exc}")

        if not got_clip and pixabay_key:
            try:
                resp = requests.get(
                    f"https://pixabay.com/api/videos/?key={pixabay_key}&q={urllib.parse.quote(query)}&per_page=8&safesearch=true",
                    timeout=20,
                )
                resp.raise_for_status()
                for h in resp.json().get("hits", []):
                    if not _query_matches_tags(query, h.get("tags", "")):
                        continue
                    videos_dict = h.get("videos", {})
                    clip_url = (
                        videos_dict.get("medium", {}).get("url")
                        or videos_dict.get("small", {}).get("url")
                        or videos_dict.get("large", {}).get("url")
                    )
                    if clip_url:
                        with requests.get(clip_url, stream=True, timeout=60) as r:
                            r.raise_for_status()
                            with open(clip_path, "wb") as f:
                                for chunk in r.iter_content(chunk_size=8192):
                                    f.write(chunk)
                        got_clip = True
                        break
            except Exception as exc:
                print(f"[WARN] Pixabay video fetch failed for '{query}': {exc}")

        # Relevant Photo + Ken Burns fallback if no matching video was found (never download unrelated stock!)
        if not got_clip and pixabay_key:
            photo_tmp = out_dir / f"raw_photo_{idx}.jpg"
            if fetch_short_topic_image(query, photo_tmp):
                try:
                    subprocess.run(
                        [
                            "ffmpeg", "-y", "-loop", "1", "-i", str(photo_tmp),
                            "-vf", "scale=1200:1400:force_original_aspect_ratio=increase,crop=1080:1240,zoompan=z='min(zoom+0.0015,1.15)':d=180:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1240,fps=30,format=yuv420p",
                            "-t", "6", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", str(clip_path),
                        ],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    got_clip = True
                except Exception as exc:
                    print(f"[WARN] Photo zoom fallback failed for '{query}': {exc}")

        if got_clip:
            downloaded_clips.append(clip_path)

    if not downloaded_clips:
        print("[INFO] Using clean animated gradient backgrounds via FFmpeg...")
        colors = ["#132238", "#1B3B5F", "#0F2C4C", "#162A45"]
        for idx, color in enumerate(colors):
            clip_path = out_dir / f"raw_clip_{idx}.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-f", "lavfi",
                    "-i", f"color=c={color}:s=1080x1920:r=30:d=15",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip_path),
                ],
                check=True,
            )
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


def find_latin_font() -> str:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    return "DejaVuSans-Bold.ttf"


def _split_script_runs(text: str) -> List[Tuple[str, bool]]:
    """Splits text into (segment, is_latin) runs so English acronyms never render as tofu boxes."""
    runs: List[Tuple[str, bool]] = []
    for part in re.findall(r"[A-Za-z]+|[^A-Za-z]+", text):
        runs.append((part, bool(re.fullmatch(r"[A-Za-z]+", part))))
    return runs


def measure_mixed_width(draw: ImageDraw.ImageDraw, text: str, ml_font: ImageFont.FreeTypeFont, lat_font: ImageFont.FreeTypeFont) -> float:
    total = 0.0
    for seg, is_lat in _split_script_runs(text):
        total += draw.textlength(seg, font=lat_font if is_lat else ml_font)
    return total


def wrap_mixed_text_px(
    draw: ImageDraw.ImageDraw,
    text: str,
    ml_font: ImageFont.FreeTypeFont,
    lat_font: ImageFont.FreeTypeFont,
    max_w: int,
) -> List[str]:
    words = text.split()
    lines: List[str] = []
    cur = ""
    for w in words:
        test = f"{cur} {w}".strip()
        if measure_mixed_width(draw, test, ml_font, lat_font) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [text]


def draw_mixed_centered_lines(
    draw: ImageDraw.ImageDraw,
    center_x: int,
    center_y: int,
    lines: List[str],
    ml_font: ImageFont.FreeTypeFont,
    lat_font: ImageFont.FreeTypeFont,
    fill: Tuple[int, ...],
    line_spacing: int = 14,
) -> None:
    line_h = ml_font.size + line_spacing
    total_h = len(lines) * line_h - line_spacing
    cur_y = center_y - total_h // 2 + ml_font.size // 2
    for ln in lines:
        w = measure_mixed_width(draw, ln, ml_font, lat_font)
        cur_x = center_x - w / 2.0
        for seg, is_lat in _split_script_runs(ln):
            fnt = lat_font if is_lat else ml_font
            draw.text((cur_x, cur_y), seg, font=fnt, fill=fill, anchor="lm")
            cur_x += draw.textlength(seg, font=fnt)
        cur_y += line_h



def generate_gemini_topic_image(subject_query: str, aspect_ratio: str, out_jpg: Path) -> bool:
    """Generates a custom colorful topic image via Gemini Paid API, with automatic fallback if Free Tier."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key or not subject_query.strip():
        return False
    client = genai.Client(api_key=api_key)
    prompt = (
        f"Vibrant, ultra-colorful, high-contrast YouTube thumbnail visual illustrating: {subject_query}. "
        "Dramatic studio lighting, eye-catching composition, curiosity-sparking focal subject on the right/center, "
        "clean dark-blue space on the left for text. Strictly NO text, NO words, NO letters, NO watermarks."
    )
    for img_model in ("gemini-3.1-flash-lite-image", "gemini-2.5-flash-image"):
        try:
            resp = client.models.generate_content(
                model=img_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                    image_config=types.ImageConfig(aspect_ratio=aspect_ratio),
                ),
            )
            for cand in getattr(resp, "candidates", []) or []:
                for part in getattr(cand.content, "parts", []) or []:
                    inline = getattr(part, "inline_data", None)
                    if inline and getattr(inline, "data", None):
                        out_jpg.write_bytes(inline.data)
                        print(f"[SUCCESS] Generated custom AI thumbnail image via {img_model}!")
                        return True
        except Exception as exc:
            print(f"[INFO] Gemini image gen ({img_model}) skipped (needs AI Studio Paid Tier): {exc}")
    return False


def fetch_short_topic_image(query: str, out_jpg: Path) -> bool:
    pixabay_key = os.getenv("PIXABAY_API_KEY", "").strip()
    if not pixabay_key or not query.strip():
        return False
    try:
        r = requests.get(
            "https://pixabay.com/api/",
            params={"key": pixabay_key, "q": query.strip(), "image_type": "all", "per_page": 5, "safesearch": "true"},
            timeout=20,
        )
        for h in r.json().get("hits", []):
            url = h.get("largeImageURL") or h.get("webformatURL")
            if url:
                out_jpg.write_bytes(requests.get(url, timeout=30).content)
                return True
    except Exception as exc:
        print(f"[WARN] Pixabay short thumbnail image lookup failed for '{query}': {exc}")
    return False


def generate_curiosity_thumbnail(
    lead_clip: Path, question_ml: str, out_jpg: Path, out_vertical_png: Path, font_path: str, thumb_query: str = ""
) -> None:
    raw_frame = out_jpg.parent / "thumb_raw_frame.jpg"
    if not (generate_gemini_topic_image(thumb_query, "9:16", raw_frame) or fetch_short_topic_image(thumb_query, raw_frame)):
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
        badge_text = "ഇതിന്റെ സത്യം എന്ത്?"
        bx0 = int(target_w * 0.14)
        bx1 = int(target_w * 0.86)
        by0 = int(target_h * (0.12 if is_vertical else 0.09))
        by1 = by0 + (88 if is_vertical else 72)
        draw.rounded_rectangle([bx0, by0, bx1, by1], radius=44, fill=(255, 61, 46, 255), outline=(255, 255, 255, 255), width=4)
        draw.text((target_w // 2, (by0 + by1) // 2), badge_text, font=badge_font, fill=(255, 255, 255, 255), anchor="mm")

        lat_path = find_latin_font()
        font_sz = 68 if is_vertical else 60
        q_font = ImageFont.truetype(font_path, font_sz)
        q_lat = ImageFont.truetype(lat_path, font_sz)
        max_q_w = int(target_w * 0.84)
        q_lines = wrap_mixed_text_px(draw, question_ml, q_font, q_lat, max_q_w)[:3]
        q_y = int(target_h * (0.68 if is_vertical else 0.62))
        draw_mixed_centered_lines(draw, target_w // 2 + 4, q_y + 4, q_lines, q_font, q_lat, (0, 0, 0, 230), line_spacing=20)
        draw_mixed_centered_lines(draw, target_w // 2, q_y, q_lines, q_font, q_lat, (255, 235, 59, 255), line_spacing=20)

        brand_font = ImageFont.truetype(font_path, 34 if is_vertical else 28)
        draw.text(
            (target_w // 2, int(target_h * 0.90)),
            "ഡെയിലി ലോജിക് മലയാളം",
            font=brand_font,
            fill=(0, 229, 255, 255),
            anchor="mm",
        )
        return canvas.convert("RGB")

    # Shorts, IG Reels, and FB Reels all use 9:16 vertical thumbnails (1080x1920)
    vertical_card = build_card(1080, 1920, True)
    vertical_card.save(out_jpg, quality=92)
    vertical_card.save(out_vertical_png)


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
    lat_path = find_latin_font()
    font_sz = 42
    ml_f = ImageFont.truetype(font_path, font_sz)
    lat_f = ImageFont.truetype(lat_path, font_sz)
    lines = wrap_mixed_text_px(draw, headline, ml_f, lat_f, 860)[:2]

    draw.rounded_rectangle(
        [60, 20, 1020, 240],
        radius=28,
        fill=(19, 34, 56, 228),
        outline=(0, 229, 255, 230),
        width=3,
    )
    draw_mixed_centered_lines(draw, 540, 130, lines, ml_f, lat_f, (255, 255, 255, 255), line_spacing=14)
    img.save(out_png)


def render_malayalam_subtitle_png(text: str, out_png: Path, font_path: str) -> None:
    img = Image.new("RGBA", (1080, 360), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    lat_path = find_latin_font()
    font_sz = 40
    ml_f = ImageFont.truetype(font_path, font_sz)
    lat_f = ImageFont.truetype(lat_path, font_sz)
    lines = wrap_mixed_text_px(draw, text, ml_f, lat_f, 880)[:3]

    max_lw = max(measure_mixed_width(draw, ln, ml_f, lat_f) for ln in lines)
    line_h = font_sz + 16
    th = len(lines) * line_h - 10
    box_w = min(980, int(max_lw) + 72)
    box_h = min(320, th + 54)
    x0 = (1080 - box_w) // 2
    y0 = (360 - box_h) // 2

    draw.rounded_rectangle(
        [x0, y0, x0 + box_w, y0 + box_h],
        radius=24,
        fill=(12, 18, 34, 220),
        outline=(0, 229, 255, 190),
        width=2,
    )
    draw_mixed_centered_lines(draw, 540, 180, lines, ml_f, lat_f, (255, 235, 59, 255), line_spacing=16)
    img.save(out_png)



# ---------------------------------------------------------------------------
# Procedural Royalty-Free BGM, SFX & 2D Motion Graphics Helpers
# ---------------------------------------------------------------------------
def synthesize_ambient_bgm(out_wav: Path, duration_sec: float, sample_rate: int = 44100) -> None:
    """Generates a soft, warm royalty-free ambient curiosity pad (C-Am-F-G)."""
    total_samples = int(duration_sec * sample_rate)
    chords = [
        (261.63, 329.63, 392.00, 493.88),  # Cmaj7
        (220.00, 261.63, 329.63, 392.00),  # Am7
        (174.61, 220.00, 261.63, 329.63),  # Fmaj7
        (196.00, 246.94, 293.66, 329.63),  # G6
    ]
    chord_len = 4.0  # seconds per chord
    fade_samples = int(1.0 * sample_rate)

    with wave.open(str(out_wav), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)

        chunk_size = 4096
        for start in range(0, total_samples, chunk_size):
            end = min(total_samples, start + chunk_size)
            buf = bytearray()
            for i in range(start, end):
                t = i / sample_rate
                chord_idx = int(t // chord_len) % len(chords)
                local_t = t % chord_len
                # Smooth crossfade envelope within each chord
                env = math.sin(math.pi * min(1.0, local_t / chord_len)) ** 0.45
                if i < fade_samples:
                    env *= i / fade_samples
                elif i > total_samples - fade_samples:
                    env *= max(0.0, (total_samples - i) / fade_samples)

                freqs = chords[chord_idx]
                val = 0.0
                for idx_f, f in enumerate(freqs):
                    val += math.sin(2.0 * math.pi * f * t) * (0.28 if idx_f == 0 else 0.18)
                # Subtle warm shimmer
                val += 0.06 * math.sin(2.0 * math.pi * (freqs[0] * 2.0) * t)
                sample = max(-1.0, min(1.0, val * env * 0.35))
                pcm = int(sample * 32767)
                buf.extend(struct.pack("<hh", pcm, pcm))
            wf.writeframes(bytes(buf))


def synthesize_sfx_whoosh(out_wav: Path, duration_sec: float = 0.42, sample_rate: int = 44100) -> None:
    """Generates a soft transition whoosh sound effect."""
    total_samples = int(duration_sec * sample_rate)
    rng = random.Random(42)
    prev = 0.0
    with wave.open(str(out_wav), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        buf = bytearray()
        for i in range(total_samples):
            t = i / total_samples
            env = math.sin(math.pi * t) ** 1.5
            noise = rng.uniform(-1.0, 1.0)
            # Simple low-pass smoothing that opens up in the middle
            alpha = 0.08 + 0.25 * math.sin(math.pi * t)
            prev = prev + alpha * (noise - prev)
            pcm = int(max(-1.0, min(1.0, prev * env * 0.55)) * 32767)
            buf.extend(struct.pack("<hh", pcm, pcm))
        wf.writeframes(bytes(buf))


def synthesize_sfx_pop(out_wav: Path, duration_sec: float = 0.16, sample_rate: int = 44100) -> None:
    """Generates a clean UI pop sound effect for 2D card entries."""
    total_samples = int(duration_sec * sample_rate)
    with wave.open(str(out_wav), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        buf = bytearray()
        for i in range(total_samples):
            t = i / sample_rate
            p = i / total_samples
            freq = 480.0 + 420.0 * (1.0 - p)
            env = math.exp(-18.0 * t) * math.sin(math.pi * min(1.0, p * 4.0))
            pcm = int(max(-1.0, min(1.0, math.sin(2.0 * math.pi * freq * t) * env * 0.6)) * 32767)
            buf.extend(struct.pack("<hh", pcm, pcm))
        wf.writeframes(bytes(buf))


def _draw_2d_animation_scene(d: ImageDraw.ImageDraw, scene_type: str, t: float, progress: float) -> None:
    """Draws real 2D vector mechanism animations cleanly in the center stage (y=460..1240)."""
    cx, cy = 540, 860

    if scene_type == "wheel_brake":
        # Wet road + rain streaks
        d.line([(80, 1130), (1000, 1130)], fill=(0, 229, 255), width=6)
        for r_i in range(10):
            rx = 140 + ((r_i * 95 + int(t * 220)) % 820)
            ry = 480 + ((r_i * 110 + int(t * 480)) % 620)
            d.line([(rx, ry), (rx - 12, ry + 32)], fill=(56, 189, 248), width=3)

        # Rotating tire + brake disc + pulsing ABS brake caliper
        r_outer, r_rim, r_disc = 230, 155, 105
        d.ellipse([cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer], fill=(24, 30, 44), outline=(148, 163, 184), width=10)
        d.ellipse([cx - r_rim, cy - r_rim, cx + r_rim, cy + r_rim], fill=(38, 52, 82), outline=(203, 213, 225), width=5)
        angle = t * 4.5
        for sp in range(6):
            a = angle + sp * (math.pi / 3.0)
            d.line([(cx, cy), (cx + int(r_rim * math.cos(a)), cy + int(r_rim * math.sin(a)))], fill=(148, 163, 184), width=8)
        d.ellipse([cx - r_disc, cy - r_disc, cx + r_disc, cy + r_disc], fill=(71, 85, 105), outline=(226, 232, 240), width=4)

        # Pulsing ABS Brake Caliper (rapidly clamps & releases)
        pulse_on = int(t * 7.0) % 2 == 0
        cal_color = (239, 68, 68) if pulse_on else (250, 204, 21)
        clamp_off = 0 if pulse_on else 14
        d.rounded_rectangle([cx + 70 + clamp_off, cy - 80, cx + 150 + clamp_off, cy + 80], radius=18, fill=cal_color, outline=(255, 255, 255), width=4)
        # Grip indicator bar
        d.rounded_rectangle([240, 1170, 840, 1196], radius=12, fill=(30, 41, 59))
        d.rounded_rectangle([240, 1170, 240 + int(600 * (0.45 + 0.5 * progress)), 1196], radius=12, fill=(74, 222, 128))

    elif scene_type == "sound_waves":
        # Two opposing sine waves cancelling out
        pts1, pts2, pts_sum = [], [], []
        for x in range(120, 960, 14):
            phase = (x - 120) * 0.028 - t * 6.0
            y1 = cy - 150 + int(70 * math.sin(phase))
            y2 = cy + 20 + int(70 * math.sin(phase + math.pi * min(1.0, progress * 1.3)))
            ys = cy + 190 + int(70 * (1.0 - min(1.0, progress * 1.3)) * math.sin(phase))
            pts1.append((x, y1))
            pts2.append((x, y2))
            pts_sum.append((x, ys))
        d.line(pts1, fill=(248, 113, 113), width=6)
        d.line(pts2, fill=(0, 229, 255), width=6)
        d.line(pts_sum, fill=(74, 222, 128), width=7)

    elif scene_type == "battery_heat":
        # Animated battery + thermal level
        d.rounded_rectangle([340, 560, 740, 1120], radius=32, fill=(18, 30, 58), outline=(0, 229, 255), width=6)
        d.rounded_rectangle([470, 520, 610, 565], radius=10, fill=(0, 229, 255))
        bars = int(1 + 4 * ((t * 0.8) % 1.0))
        for b in range(4):
            by0 = 980 - b * 120
            col = (239, 68, 68) if b == 3 else (74, 222, 128)
            if b < bars:
                d.rounded_rectangle([380, by0, 700, by0 + 95], radius=16, fill=col)

    elif scene_type == "pressure_flow":
        # Animated vessel + rising steam/flow particles
        d.rounded_rectangle([310, 720, 770, 1120], radius=36, fill=(20, 36, 68), outline=(56, 189, 248), width=6)
        d.rectangle([480, 640, 600, 725], fill=(250, 204, 21))
        for p_i in range(14):
            px = 370 + ((p_i * 53) % 340)
            py = 1080 - int(((t * 240 + p_i * 65) % 480))
            d.ellipse([px - 12, py - 12, px + 12, py + 12], fill=(0, 229, 255) if py > 720 else (255, 255, 255))

    elif scene_type == "gear_mechanism":
        # Two interlocking rotating gears
        for g_cx, g_cy, rad, teeth, dir_s, col in [(420, 820, 150, 10, 1.0, (0, 229, 255)), (665, 930, 115, 8, -1.25, (250, 204, 21))]:
            d.ellipse([g_cx - rad, g_cy - rad, g_cx + rad, g_cy + rad], outline=col, width=10)
            d.ellipse([g_cx - 38, g_cy - 38, g_cx + 38, g_cy + 38], fill=col)
            for k in range(teeth):
                ang = dir_s * t * 2.2 + k * (2 * math.pi / teeth)
                x0 = g_cx + int((rad - 8) * math.cos(ang))
                y0 = g_cy + int((rad - 8) * math.sin(ang))
                x1 = g_cx + int((rad + 28) * math.cos(ang))
                y1 = g_cy + int((rad + 28) * math.sin(ang))
                d.line([(x0, y0), (x1, y1)], fill=col, width=16)

    elif scene_type == "light_optics":
        # Light beam hitting a prism/mirror and splitting into spectrum rays
        d.polygon([(540, 630), (360, 1030), (720, 1030)], fill=(22, 42, 82), outline=(0, 229, 255), width=6)
        d.line([(100, 790), (450, 830)], fill=(255, 255, 255), width=10)
        ray_cols = [(239, 68, 68), (250, 204, 21), (74, 222, 128), (56, 189, 248)]
        for idx_r, rc in enumerate(ray_cols):
            shift = int(20 * math.sin(t * 4.0 + idx_r))
            d.line([(630, 830), (980, 730 + idx_r * 75 + shift)], fill=rc, width=8)

    elif scene_type == "heart_pulse":
        # Animated ECG waveform
        pts = []
        for x in range(110, 970, 12):
            pos = ((x + int(t * 320)) % 220) / 220.0
            dy = 0
            if 0.40 < pos < 0.48:
                dy = -190
            elif 0.48 <= pos < 0.56:
                dy = 120
            pts.append((x, cy + dy))
        d.line(pts, fill=(74, 222, 128), width=8)

    else:
        # comparison_bars: animated 3-bar visual comparison
        bar_cols = [(248, 113, 113), (250, 204, 21), (74, 222, 128)]
        targets = [0.45, 0.72, 0.95]
        for b_i, (bc, tgt) in enumerate(zip(bar_cols, targets)):
            bx0 = 230 + b_i * 230
            bh = int(420 * tgt * min(1.0, progress * 1.4))
            d.rounded_rectangle([bx0, 1080 - bh, bx0 + 130, 1080], radius=16, fill=bc)


def render_2d_motion_card_clip(
    card_title: str,
    card_body_ml: str,
    card_index: int,
    duration_sec: float,
    out_mp4: Path,
    font_path: str,
    animation_scene: str = "gear_mechanism",
) -> None:
    """Renders a 1080x1920 30fps 2D vector animation clip in the center stage (no subtitle overlap!)."""
    fps = 30
    total_frames = max(60, int(round(duration_sec * fps)))
    W, H = 1080, 1920
    lat_path = find_latin_font()

    bg = Image.new("RGB", (W, H), (10, 17, 36))
    bg_draw = ImageDraw.Draw(bg)
    for gx in range(0, W, 90):
        bg_draw.line([(gx, 0), (gx, H)], fill=(18, 32, 62), width=2)
    for gy in range(0, H, 90):
        bg_draw.line([(0, gy), (W, gy)], fill=(18, 32, 62), width=2)

    title_font = ImageFont.truetype(font_path, 38)
    title_lat = ImageFont.truetype(lat_path, 38)
    accent = (0, 229, 255) if card_index % 2 == 0 else (255, 214, 0)

    # Short 1-line label sits cleanly ABOVE the animation (y=410..480), never covering it
    label_lines = wrap_mixed_text_px(bg_draw, card_body_ml, title_font, title_lat, 860)[:1]

    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{W}x{H}", "-pix_fmt", "rgb24", "-r", str(fps),
        "-i", "-",
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", str(out_mp4),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert proc.stdin is not None

    try:
        for f in range(total_frames):
            t = f / fps
            progress = f / max(1, total_frames - 1)
            frame = bg.copy()
            d = ImageDraw.Draw(frame)

            # Clean border around the 2D animation stage (y=390..1280) -> subtitles stay safely at y=1390!
            d.rounded_rectangle([65, 390, 1015, 1280], radius=32, fill=(13, 23, 46), outline=accent, width=4)
            draw_mixed_centered_lines(d, 540, 445, label_lines, title_font, title_lat, accent, line_spacing=8)

            _draw_2d_animation_scene(d, animation_scene, t, progress)

            # Clean progress bar at bottom of stage
            d.rounded_rectangle([120, 1236, 960, 1252], radius=8, fill=(30, 46, 80))
            d.rounded_rectangle([120, 1236, 120 + max(16, int(840 * progress)), 1252], radius=8, fill=accent)

            proc.stdin.write(frame.tobytes())
    finally:
        proc.stdin.close()
        ret = proc.wait()
        if ret != 0:
            raise RuntimeError(f"FFmpeg 2D card render failed with exit code {ret}")


def assemble_short_video(
    clips: List[Path],
    audio_path: Path,
    srt_path: Path,
    hook_headline: str,
    key_points_ml: List[str],
    animation_scene: str,
    thumb_vertical_png: Path,
    final_video_path: Path,
    work_dir: Path,
) -> None:
    audio_duration = get_media_duration(audio_path)
    font_path = find_malayalam_font()
    # Interleave up to 2 animated 2D explainer cards among stock clips
    anim_cards: List[Path] = []
    clean_points = [pt.strip() for pt in (key_points_ml or []) if pt.strip()][:2]
    total_segments = len(clips) + len(clean_points)
    segment_duration = max(3.5, (audio_duration + 0.5) / max(1, total_segments))

    for c_idx, pt_text in enumerate(clean_points):
        card_mp4 = work_dir / f"anim_card_{c_idx}.mp4"
        print(f"      Rendering 2D animated explainer card {c_idx + 1}/{len(clean_points)}...")
        render_2d_motion_card_clip(
            card_title=f"പ്രധാന കാര്യം #{c_idx + 1}",
            card_body_ml=pt_text,
            card_index=c_idx,
            duration_sec=segment_duration,
            out_mp4=card_mp4,
            font_path=font_path,
            animation_scene=animation_scene,
        )
        anim_cards.append(card_mp4)

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

    # Interleave 2D motion cards after clip 0 and clip 1 for dynamic pacing
    interleaved_clips: List[Path] = []
    card_cursor = 0
    for idx, norm_c in enumerate(normalized_clips):
        interleaved_clips.append(norm_c)
        if card_cursor < len(anim_cards) and idx in (0, 1):
            interleaved_clips.append(anim_cards[card_cursor])
            card_cursor += 1
    while card_cursor < len(anim_cards):
        interleaved_clips.append(anim_cards[card_cursor])
        card_cursor += 1
    normalized_clips = interleaved_clips

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

    header_png = work_dir / "header.png"
    render_malayalam_header_png(hook_headline, header_png, font_path)

    # Synthesize royalty-free ambient BGM + transition SFX and mix with voiceover
    bgm_wav = work_dir / "bgm.wav"
    whoosh_wav = work_dir / "whoosh.wav"
    pop_wav = work_dir / "pop.wav"
    mixed_audio_wav = work_dir / "mixed_audio.wav"
    synthesize_ambient_bgm(bgm_wav, audio_duration + 1.0)
    synthesize_sfx_whoosh(whoosh_wav)
    synthesize_sfx_pop(pop_wav)

    sfx_inputs = ["-i", str(audio_path), "-i", str(bgm_wav), "-i", str(whoosh_wav), "-i", str(pop_wav)]
    sfx_filters = [
        "[0:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,volume=1.0[vo]",
        "[1:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,volume=0.11[bgm]",
    ]
    mix_labels = ["[vo]", "[bgm]"]
    # Add whoosh on segment transitions and pop on 2D card entries
    seg_times = [round(i * segment_duration, 2) for i in range(1, len(normalized_clips)) if i * segment_duration < audio_duration]
    for s_i, st in enumerate(seg_times):
        ms = int(st * 1000)
        lbl = f"[w{s_i}]"
        sfx_filters.append(f"[2:a]adelay={ms}|{ms},volume=0.22{lbl}")
        mix_labels.append(lbl)
    card_times = [round(segment_duration, 2), round(3 * segment_duration, 2)]
    for p_i, ct in enumerate(card_times[:len(anim_cards)]):
        if ct < audio_duration:
            ms = int(ct * 1000) + 120
            lbl = f"[p{p_i}]"
            sfx_filters.append(f"[3:a]adelay={ms}|{ms},volume=0.28{lbl}")
            mix_labels.append(lbl)

    sfx_filters.append(
        f"{''.join(mix_labels)}amix=inputs={len(mix_labels)}:duration=first:dropout_transition=2:normalize=0[aout]"
    )
    subprocess.run(
        [
            "ffmpeg", "-y", *sfx_inputs,
            "-filter_complex", ";".join(sfx_filters),
            "-map", "[aout]", "-ac", "2", "-ar", "44100", str(mixed_audio_wav),
        ],
        check=True,
    )

    cmd_inputs = [
        "-i", str(merged_bg_path),
        "-i", str(mixed_audio_wav),
        "-i", str(header_png),
    ]
    filter_parts = [
        "[0:v][2:v]overlay=0:90[vhead]",
    ]
    last_label = "[vhead]"

    for idx, (start_t, end_t, text) in enumerate(cues):
        png_path = work_dir / f"sub_{idx}.png"
        render_malayalam_subtitle_png(text, png_path, font_path)
        cmd_inputs.extend(["-i", str(png_path)])
        input_idx = idx + 3
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

    status_obj = {
        "privacyStatus": PRIVACY_STATUS,
        "selfDeclaredMadeForKids": False,
        "containsSyntheticMedia": False,
    }
    if PRIVACY_STATUS == "private":
        # Schedule auto-publish at 7:00 PM IST (13:30 UTC) if run before 6:50 PM IST, else +60 min
        ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        target_ist = ist_now.replace(hour=19, minute=0, second=0, microsecond=0)
        if ist_now >= target_ist - timedelta(minutes=10):
            target_ist = ist_now + timedelta(minutes=60)
        status_obj["publishAt"] = target_ist.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"[INFO] YouTube scheduled for auto-publish at {status_obj['publishAt']} (7:00 PM IST).")

    body = {
        "snippet": {
            "title": plan.title[:100],
            "description": plan.description,
            "tags": plan.tags,
            "categoryId": CATEGORY_ID,
            "defaultLanguage": "ml",
            "defaultAudioLanguage": "ml",
        },
        "status": status_obj,
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

    # Wait for YouTube video processing to finish + 10s cooling period before setting custom thumbnail
    for _ in range(16):
        try:
            vinfo = youtube.videos().list(part="processingDetails", id=video_id).execute()
            pstat = (
                vinfo.get("items", [{}])[0]
                .get("processingDetails", {})
                .get("processingStatus", "")
            )
            print(f"[INFO] YouTube Shorts processingStatus: {pstat or 'pending'}")
            if pstat == "succeeded":
                time.sleep(10)
                break
            if pstat in ("failed", "terminated"):
                break
        except Exception:
            pass
        time.sleep(15)

    try:
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(str(thumb_jpg), mimetype="image/jpeg"),
        ).execute()
        print("[SUCCESS] Custom curiosity thumbnail uploaded to YouTube Shorts!")
    except Exception as exc:
        print(f"[WARN] Custom thumbnail API skipped: {exc}")

    try:
        pls = youtube.playlists().list(part="snippet", mine=True, maxResults=50).execute()
        pl_id = next(
            (i["id"] for i in pls.get("items", []) if i["snippet"]["title"].strip().lower() == "daily logic malayalam - shorts"),
            None,
        )
        if not pl_id:
            pl_id = youtube.playlists().insert(
                part="snippet,status",
                body={
                    "snippet": {"title": "Daily Logic Malayalam - Shorts", "description": "Daily Logic Malayalam Shorts"},
                    "status": {"privacyStatus": "public"},
                },
            ).execute()["id"]
        youtube.playlistItems().insert(
            part="snippet",
            body={"snippet": {"playlistId": pl_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}},
        ).execute()
        print("[SUCCESS] Added Short to playlist: Daily Logic Malayalam - Shorts")
    except Exception as exc:
        print(f"[INFO] Shorts playlist skipped (needs youtube or youtube.force-ssl scope): {exc}")

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

    init_url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{ig_user_id}/media"
    container_id = None

    # Primary method: use GitHub Release public URL if provided, else stage on temporary URL
    # (avoids Meta's intermittent rupload.facebook.com/ig-api-upload ProcessingFailedError)
    public_video_url = os.getenv("IG_PUBLIC_VIDEO_URL", "").strip() or None
    if public_video_url:
        print(f"[INFO] Using GitHub Release public URL for Instagram: {public_video_url}")
    staging_hosts = [
        ("tmpfiles.org", "https://tmpfiles.org/api/v1/upload"),
        ("litterbox.catbox.moe", "https://litterbox.catbox.moe/resources/internals/api.php"),
        ("0x0.st", "https://0x0.st"),
    ]
    for host_name, host_url in ([] if public_video_url else staging_hosts):
        try:
            with open(video_path, "rb") as f:
                if host_name == "tmpfiles.org":
                    resp = requests.post(
                        host_url,
                        files={"file": ("final_short.mp4", f, "video/mp4")},
                        timeout=120,
                    )
                    if resp.ok:
                        raw_url = resp.json().get("data", {}).get("url", "")
                        if raw_url:
                            raw_url = raw_url.replace("http://", "https://", 1)
                            public_video_url = raw_url.replace("tmpfiles.org/", "tmpfiles.org/dl/", 1)
                elif host_name == "litterbox.catbox.moe":
                    resp = requests.post(
                        host_url,
                        data={"reqtype": "fileupload", "time": "1h"},
                        files={"fileToUpload": ("final_short.mp4", f, "video/mp4")},
                        timeout=120,
                    )
                    if resp.ok and resp.text.strip().startswith("https://"):
                        public_video_url = resp.text.strip()
                else:
                    resp = requests.post(
                        host_url,
                        headers={"User-Agent": "DailyLogicBot/1.0"},
                        files={"file": ("final_short.mp4", f, "video/mp4")},
                        timeout=120,
                    )
                    if resp.ok and resp.text.strip().startswith("https://"):
                        public_video_url = resp.text.strip()
            if public_video_url:
                print(f"[INFO] Staged temporary public URL via {host_name}: {public_video_url}")
                break
            print(f"[WARN] {host_name} returned HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as exc:
            print(f"[WARN] {host_name} staging failed: {exc}")

    if public_video_url:
        ig_payload = {
            "media_type": "REELS",
            "video_url": public_video_url,
            "caption": caption,
            "share_to_feed": "true",
            "access_token": access_token,
        }
        public_thumb_url = os.getenv("IG_PUBLIC_THUMB_URL", "").strip()
        if public_thumb_url:
            ig_payload["cover_url"] = public_thumb_url
            print(f"[INFO] Using separate Instagram cover_url: {public_thumb_url}")
        else:
            ig_payload["thumb_offset"] = "0"
        init_resp = requests.post(init_url, data=ig_payload, timeout=30)
        if not init_resp.ok:
            print(f"[ERROR] Instagram video_url init HTTP {init_resp.status_code}: {init_resp.text}")
            init_resp.raise_for_status()
        container_id = init_resp.json()["id"]
    else:
        video_bytes = video_path.read_bytes()
        file_size = len(video_bytes)
        for attempt in range(1, 3):
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

    # If META_ACCESS_TOKEN is a User Token, automatically exchange it for the Page Access Token
    page_token = access_token
    try:
        pt_resp = requests.get(
            f"https://graph.facebook.com/{GRAPH_API_VERSION}/{fb_page_id}",
            params={"fields": "access_token", "access_token": access_token},
            timeout=20,
        )
        if pt_resp.ok and pt_resp.json().get("access_token"):
            page_token = pt_resp.json()["access_token"]
            print("[INFO] Resolved Page Access Token for Facebook Reels.")
    except Exception as exc:
        print(f"[WARN] Page token lookup skipped ({exc}); using META_ACCESS_TOKEN directly.")

    access_token = page_token
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
    thumb_jpg = WORK_DIR / "thumbnail.jpg"
    if thumb_jpg.exists():
        try:
            with open(thumb_jpg, "rb") as tf:
                th_resp = requests.post(
                    f"https://graph.facebook.com/{GRAPH_API_VERSION}/{video_id}/thumbnails",
                    data={"is_preferred": "true", "access_token": access_token},
                    files={"source": ("thumbnail.jpg", tf, "image/jpeg")},
                    timeout=30,
                )
            if th_resp.ok:
                print("[SUCCESS] Separate custom thumbnail attached to Facebook Reel!")
            else:
                print(f"[INFO] Facebook Reel thumbnail API skipped (HTTP {th_resp.status_code}): {th_resp.text[:200]}")
        except Exception as exc:
            print(f"[INFO] Facebook Reel thumbnail skipped: {exc}")


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
        clips[0], plan.thumbnail_question_ml, thumb_jpg, thumb_vertical_png, font_path, plan.thumbnail_image_query
    )
    assemble_short_video(
        clips,
        audio_path,
        srt_path,
        plan.hook_headline_ml,
        plan.key_points_ml,
        plan.animation_scene,
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