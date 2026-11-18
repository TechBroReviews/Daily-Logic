#!/usr/bin/env python3
"""
Daily Automated Malayalam Everyday Science, Fact-Check & "How Things Work" Video Generator
Platforms Supported:
  1. YouTube Shorts (via YouTube Data API v3)
  2. Instagram Reels (via Meta Graph API v21.0 Resumable Upload)
  3. Facebook Page Reels (via Meta Graph API v21.0 Page Video Reels API)

Stack:
  - Topic Discovery: Everyday Science, HowStuffWorks, Health & Tech RSS Feeds + Evergreen Bank
  - Malayalam Script & Metadata: Google Gemini API (google-genai SDK)
  - Malayalam Neural Voiceover & Subtitles: Microsoft Edge TTS (ml-IN-MidhunNeural / ml-IN-SobhanaNeural)
  - Stock Footage: Supports Pixabay Video API (PIXABAY_API_KEY), Pexels Video API (PEXELS_API_KEY),
    OR Keyless Wikimedia Commons Free Videos (if neither key is provided!)
  - Video Assembly: FFmpeg (1080x1920 crop/scale, Noto Sans Malayalam burned-in SRT, audio sync)

Required Environment Variables:
  - GEMINI_API_KEY
  - YT_CLIENT_ID
  - YT_CLIENT_SECRET
  - YT_REFRESH_TOKEN
Optional Stock Video Keys (works even if both are omitted via Wikimedia Commons fallback):
  - PIXABAY_API_KEY
  - PEXELS_API_KEY
Optional Meta Reels Keys:
  - META_ACCESS_TOKEN
  - IG_USER_ID
  - FB_PAGE_ID
"""

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import edge_tts
import requests
from google import genai
from google.genai import types
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Configuration (Everyday Science, How Things Work & Daily Life Fact-Checking)
# ---------------------------------------------------------------------------
RSS_FEEDS = [
    "https://syndication.howstuffworks.com/rss/HSW",
    "https://www.sciencedaily.com/rss/health_medicine.xml",
    "https://www.sciencedaily.com/rss/matter_energy.xml",
    "https://www.sciencedaily.com/rss/mind_brain.xml",
    "https://www.wired.com/feed/category/science/latest/rss",
]

EVERGREEN_DAILY_TOPICS = [
    "How a Microwave Oven Actually Heats Your Food From the Inside Out (Myth vs Reality)",
    "Why Pressure Cookers Cook Food So Fast: The Everyday Physics Inside Your Kitchen",
    "Fact Check: Does Fast Charging Actually Ruin Your Smartphone Battery?",
    "How Noise-Canceling Headphones Create Silence Using Sound Waves",
    "Why Onions Make You Cry and the Scientific Trick to Stop It Instantly",
    "How Induction Stoves Heat Metal Pots While Staying Cool to the Touch",
    "Fact Check: Does Cracking Your Knuckles Really Cause Arthritis?",
    "How Non-Stick Pans Work and When They Become Unsafe to Use",
    "Why Soap and Water Destroy Viruses and Bacteria Better Than Sanitizer",
    "How Refrigerator Cooling Works: Taking Heat Out Instead of Putting Cold In",
    "Fact Check: Why Do Mosquitoes Bite Certain People More Than Others?",
    "How Touchscreen Phones Detect Your Finger Instantly Through Glass",
    "Why Coffee Keeps You Awake: How Caffeine Tricks Your Brain's Tiredness Sensors",
    "How Car Airbags Inflate in 30 Milliseconds Using a Tiny Chemical Reaction",
    "Fact Check: Does Drinking Warm Water After Meals Melt Fat or Help Digestion?",
]

HISTORY_FILE = Path("used_topics.json")
WORK_DIR = Path("build_short")
VOICE_NAME = os.getenv("TTS_VOICE", "ml-IN-MidhunNeural")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
PRIVACY_STATUS = os.getenv("YT_PRIVACY_STATUS", "public")
CATEGORY_ID = os.getenv("YT_CATEGORY_ID", "28")
GRAPH_API_VERSION = "v21.0"


# ---------------------------------------------------------------------------
# Structured Output Schema for Gemini
# ---------------------------------------------------------------------------
class ShortScriptPlan(BaseModel):
    title: str = Field(
        description="Catchy Malayalam + English YouTube Shorts title under 70 characters about everyday science or fact-checking, including #Shorts #Malayalam."
    )
    description: str = Field(
        description="Engaging Malayalam & English description explaining the daily-life science fact or myth-bust, plus 5-8 hashtags (#DailyLogicMalayalam #MalayalamFacts #ScienceMalayalam #FactCheckMalayalam #Shorts)."
    )
    tags: List[str] = Field(
        description="6 to 10 relevant SEO keywords in Malayalam and English without # symbols."
    )
    narration_malayalam: str = Field(
        description="Spoken voiceover script written strictly in Malayalam script (മലയാളം ലിപി) for natural pronunciation by ml-IN neural TTS (85 to 115 words, ~45-55 seconds). Plain spoken Malayalam text only, no stage directions or markdown."
    )
    search_queries: List[str] = Field(
        description="Exactly 4 concise 1-to-2 word ENGLISH visual search terms for stock videos showing everyday objects, human actions, or tools (e.g., 'smartphone', 'kitchen cooking', 'coffee cup', 'car driving')."
    )


# ---------------------------------------------------------------------------
# Step 1: Discover a Fresh Everyday Science Topic & Prevent Duplicates
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
    headers = {"User-Agent": "Mozilla/5.0 (DailyLogicMalayalamBot/1.0)"}

    for feed_url in RSS_FEEDS:
        try:
            resp = requests.get(feed_url, headers=headers, timeout=15)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)

            items = root.findall(".//item") or root.findall(
                ".//{http://www.w3.org/2005/Atom}entry"
            )
            for item in items:
                title_el = item.find("title")
                if title_el is None:
                    title_el = item.find("{http://www.w3.org/2005/Atom}title")
                link_el = item.find("link")
                if link_el is None:
                    link_el = item.find("{http://www.w3.org/2005/Atom}link")

                title = (title_el.text or "").strip() if title_el is not None else ""
                link = ""
                if link_el is not None:
                    link = (link_el.text or link_el.attrib.get("href", "")).strip()

                if title and title not in used_topics:
                    return {"title": title, "link": link, "used_list": used_topics}
        except Exception as exc:
            print(f"[WARN] Failed reading feed {feed_url}: {exc}")

    for topic in EVERGREEN_DAILY_TOPICS:
        if topic not in used_topics:
            return {"title": topic, "link": "", "used_list": used_topics}

    fallback = f"Everyday Science Fact Check ({datetime.now(timezone.utc).strftime('%Y-%m-%d')})"
    return {"title": fallback, "link": "", "used_list": used_topics}


# ---------------------------------------------------------------------------
# Step 2: Generate Malayalam Daily-Life Script & Visual Queries via Gemini API
# ---------------------------------------------------------------------------
def generate_script_plan(topic_title: str, topic_link: str) -> ShortScriptPlan:
    api_key = os.environ["GEMINI_API_KEY"]
    client = genai.Client(api_key=api_key)

    prompt = f"""
You are the creator of "Daily Logic Malayalam", a viral Malayalam Everyday Science & Fact-Check channel making a 50-second vertical video (YouTube Shorts, Instagram Reels, and Facebook Reels).
Raw Topic / Inspiration: {topic_title}
Reference Link: {topic_link}

CRITICAL ANGLE:
- DO NOT talk about outer space, galaxies, or abstract astrophysics.
- Connect the topic directly to DAY-TO-DAY LIFE on Earth: things people use every day at home, in the kitchen, on their phone, in their body/health, or common beliefs that need scientific fact-checking ("ഇത് എങ്ങനെ പ്രവർത്തിക്കുന്നു?" / "ഇതിന് പിന്നിലെ സത്യം എന്ത്?").
- If the raw headline is academic, translate it into a practical, relatable daily-life habit, household gadget mechanism, or myth-vs-fact check.

Rules:
1. Write 'narration_malayalam' strictly in natural, conversational Malayalam script (മലയാളം ലിപി) spoken in Kerala.
2. Start with a relatable daily-life hook in the first 3 seconds (e.g., "നമ്മൾ ദിവസവും ഉപയോഗിക്കുന്ന..." or "പലരും തെറ്റിദ്ധരിച്ചിരിക്കുന്ന ഒരു കാര്യമാണ്...").
3. Clearly explain HOW it works or the real scientific truth behind it in simple Malayalam.
4. End with a quick question for the comment section.
5. Keep 'narration_malayalam' strictly to spoken Malayalam words (no [Music], no speaker labels, no asterisks, no English letters).
6. Provide 'search_queries' in ENGLISH (4 simple 1-to-2 word everyday visual terms like 'smartphone', 'cooking kitchen', 'drinking water', 'washing hands') for stock video search.
"""

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ShortScriptPlan,
            temperature=0.7,
        ),
    )
    return ShortScriptPlan.model_validate_json(response.text)


# ---------------------------------------------------------------------------
# Step 3: Synthesize Malayalam Voiceover (.mp3) & Captions (.srt) with edge-tts
# ---------------------------------------------------------------------------
async def create_voiceover_and_srt(
    narration: str, audio_path: Path, srt_path: Path
) -> None:
    communicate = edge_tts.Communicate(narration, VOICE_NAME, rate="+3%")
    submaker = edge_tts.SubMaker()

    with open(audio_path, "wb") as audio_file:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_file.write(chunk["data"])
            elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                submaker.feed(chunk)

    srt_content = submaker.get_srt()
    srt_path.write_text(srt_content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Step 4: Download Stock Clips (Supports Pixabay, Pexels, or Keyless Fallback)
# ---------------------------------------------------------------------------
def download_stock_clips(queries: List[str], out_dir: Path) -> List[Path]:
    pexels_key = os.getenv("PEXELS_API_KEY", "").strip()
    pixabay_key = os.getenv("PIXABAY_API_KEY", "").strip()
    downloaded_clips: List[Path] = []

    # Option A: Use Pexels API if PEXELS_API_KEY is configured
    if pexels_key:
        headers = {"Authorization": pexels_key}
        for idx, query in enumerate(queries):
            encoded_q = urllib.parse.quote(query)
            url = f"https://api.pexels.com/videos/search?query={encoded_q}&orientation=portrait&size=medium&per_page=5"
            try:
                resp = requests.get(url, headers=headers, timeout=20)
                resp.raise_for_status()
                videos = resp.json().get("videos", [])
                if not videos:
                    fallback_url = "https://api.pexels.com/videos/search?query=technology%20lifestyle&orientation=portrait&per_page=5"
                    videos = requests.get(fallback_url, headers=headers, timeout=20).json().get("videos", [])

                clip_url = None
                for video in videos:
                    for vf in video.get("video_files", []):
                        w, h = vf.get("width", 0), vf.get("height", 0)
                        if h >= w and vf.get("file_type") == "video/mp4" and 720 <= w <= 1440:
                            clip_url = vf.get("link")
                            break
                    if clip_url:
                        break
                if not clip_url and videos:
                    clip_url = videos[0]["video_files"][0]["link"]

                if clip_url:
                    clip_path = out_dir / f"raw_clip_{idx}.mp4"
                    with requests.get(clip_url, stream=True, timeout=60) as r:
                        r.raise_for_status()
                        with open(clip_path, "wb") as f:
                            for chunk in r.iter_content(chunk_size=8192):
                                f.write(chunk)
                    downloaded_clips.append(clip_path)
            except Exception as exc:
                print(f"[WARN] Pexels fetch failed for '{query}': {exc}")

    # Option B: Use Pixabay Video API if PIXABAY_API_KEY is configured
    if not downloaded_clips and pixabay_key:
        for idx, query in enumerate(queries):
            encoded_q = urllib.parse.quote(query)
            url = f"https://pixabay.com/api/videos/?key={pixabay_key}&q={encoded_q}&per_page=5"
            try:
                resp = requests.get(url, timeout=20)
                resp.raise_for_status()
                hits = resp.json().get("hits", [])
                if not hits:
                    fallback_url = f"https://pixabay.com/api/videos/?key={pixabay_key}&q=science+technology&per_page=5"
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

    # Option C: Keyless Fallback (Generates smooth animated gradient background clips via FFmpeg filter)
    if not downloaded_clips:
        print("[INFO] No Pexels/Pixabay key found or clips unavailable; generating clean animated visual backgrounds via FFmpeg...")
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
# Step 5: Assemble 1080x1920 Vertical Video with FFmpeg (Malayalam Font Support)
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
    out = subprocess.check_output(cmd, text=True).strip()
    return float(out)


def assemble_short_video(
    clips: List[Path], audio_path: Path, srt_path: Path, final_video_path: Path, work_dir: Path
) -> None:
    audio_duration = get_media_duration(audio_path)
    segment_duration = max(3.5, (audio_duration + 0.5) / len(clips))

    normalized_clips: List[Path] = []
    for idx, clip in enumerate(clips):
        norm_path = work_dir / f"norm_{idx}.mp4"
        vf_chain = (
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,"
            "fps=30,"
            "format=yuv420p"
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
            "-vf",
            vf_chain,
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
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

    video_filters = []
    if srt_path.exists() and srt_path.stat().st_size > 10:
        escaped_srt = str(srt_path.resolve()).replace("\\", "/").replace(":", "\\:")
        style = (
            "FontName=Noto Sans Malayalam,FontSize=15,PrimaryColour=&H0000FFFF,"
            "OutlineColour=&H00000000,BorderStyle=3,Outline=2,Shadow=0,"
            "Alignment=2,MarginV=60"
        )
        video_filters.append(f"subtitles='{escaped_srt}':force_style='{style}'")

    cmd_final = [
        "ffmpeg",
        "-y",
        "-i",
        str(merged_bg_path),
        "-i",
        str(audio_path),
    ]
    if video_filters:
        cmd_final.extend(["-vf", ",".join(video_filters)])

    cmd_final.extend(
        [
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "22",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            str(final_video_path),
        ]
    )
    subprocess.run(cmd_final, check=True)


# ---------------------------------------------------------------------------
# Step 6A: Upload to YouTube Shorts via YouTube Data API v3
# ---------------------------------------------------------------------------
def upload_to_youtube(video_path: Path, plan: ShortScriptPlan) -> str:
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )

    youtube = build("youtube", "v3", credentials=creds)

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
    print(f"[SUCCESS] Uploaded YouTube Short! URL: https://youtube.com/shorts/{video_id}")
    return video_id


# ---------------------------------------------------------------------------
# Step 6B: Upload to Instagram Reels via Meta Graph API (Resumable Local Upload)
# ---------------------------------------------------------------------------
def upload_to_instagram_reels(video_path: Path, plan: ShortScriptPlan) -> None:
    access_token = os.getenv("META_ACCESS_TOKEN")
    ig_user_id = os.getenv("IG_USER_ID")
    if not access_token or not ig_user_id:
        print("[SKIP] Instagram Reels skipped (META_ACCESS_TOKEN or IG_USER_ID not set).")
        return

    caption = f"{plan.title}\n\n{plan.description}"
    file_size = video_path.stat().st_size

    init_url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{ig_user_id}/media"
    init_resp = requests.post(
        init_url,
        data={
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": caption,
            "share_to_feed": "true",
            "access_token": access_token,
        },
        timeout=30,
    )
    init_resp.raise_for_status()
    container_data = init_resp.json()
    container_id = container_data["id"]
    upload_uri = container_data.get(
        "uri", f"https://rupload.facebook.com/ig-api-upload/{GRAPH_API_VERSION}/{container_id}"
    )

    with open(video_path, "rb") as f:
        rupload_resp = requests.post(
            upload_uri,
            headers={
                "Authorization": f"OAuth {access_token}",
                "offset": "0",
                "file_size": str(file_size),
            },
            data=f,
            timeout=180,
        )
        rupload_resp.raise_for_status()

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
    print(f"[SUCCESS] Published Instagram Reel! Media ID: {pub_resp.json().get('id')}")


# ---------------------------------------------------------------------------
# Step 6C: Upload to Facebook Page Reels via Meta Video Reels API
# ---------------------------------------------------------------------------
def upload_to_facebook_reels(video_path: Path, plan: ShortScriptPlan) -> None:
    access_token = os.getenv("META_ACCESS_TOKEN")
    fb_page_id = os.getenv("FB_PAGE_ID")
    if not access_token or not fb_page_id:
        print("[SKIP] Facebook Reels skipped (META_ACCESS_TOKEN or FB_PAGE_ID not set).")
        return

    description = f"{plan.title}\n\n{plan.description}"
    file_size = video_path.stat().st_size

    start_url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{fb_page_id}/video_reels"
    start_resp = requests.post(
        start_url,
        data={"upload_phase": "start", "access_token": access_token},
        timeout=30,
    )
    start_resp.raise_for_status()
    start_data = start_resp.json()
    video_id = start_data["video_id"]
    upload_url = start_data.get(
        "upload_url", f"https://rupload.facebook.com/video-upload/{GRAPH_API_VERSION}/{video_id}"
    )

    with open(video_path, "rb") as f:
        up_resp = requests.post(
            upload_url,
            headers={
                "Authorization": f"OAuth {access_token}",
                "offset": "0",
                "file_size": str(file_size),
            },
            data=f,
            timeout=180,
        )
        up_resp.raise_for_status()

    finish_resp = requests.post(
        start_url,
        data={
            "upload_phase": "finish",
            "video_id": video_id,
            "video_state": "PUBLISHED",
            "description": description,
            "title": plan.title[:100],
            "access_token": access_token,
        },
        timeout=30,
    )
    finish_resp.raise_for_status()
    print(f"[SUCCESS] Published Facebook Page Reel! Video ID: {video_id}")


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
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
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

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    audio_path = WORK_DIR / "voiceover.mp3"
    srt_path = WORK_DIR / "captions.srt"
    final_video_path = WORK_DIR / "final_short.mp4"

    print("[1/7] Finding a fresh Everyday Science / Fact-Check topic...")
    topic_info = fetch_daily_topic()
    print(f"      Selected topic: {topic_info['title']}")

    print("[2/7] Generating Malayalam daily-life script & metadata with Gemini...")
    plan = generate_script_plan(topic_info["title"], topic_info["link"])
    print(f"      Title: {plan.title}")
    print(f"      Search Queries: {plan.search_queries}")

    print(f"[3/7] Synthesizing Malayalam voiceover ({VOICE_NAME}) & SRT with edge-tts...")
    asyncio.run(create_voiceover_and_srt(plan.narration_malayalam, audio_path, srt_path))

    print("[4/7] Downloading stock video clips (Pixabay / Pexels / FFmpeg fallback)...")
    clips = download_stock_clips(plan.search_queries, WORK_DIR)

    print("[5/7] Rendering 1080x1920 vertical video with FFmpeg...")
    assemble_short_video(clips, audio_path, srt_path, final_video_path, WORK_DIR)

    print("[6/7] Uploading to YouTube Shorts...")
    upload_to_youtube(final_video_path, plan)

    print("[7/7] Cross-posting to Instagram Reels & Facebook Page Reels...")
    try:
        upload_to_instagram_reels(final_video_path, plan)
    except Exception as exc:
        print(f"[WARN] Instagram Reels upload failed: {exc}")

    try:
        upload_to_facebook_reels(final_video_path, plan)
    except Exception as exc:
        print(f"[WARN] Facebook Reels upload failed: {exc}")

    save_used_topic(topic_info["title"], topic_info["used_list"])
    print("[DONE] Daily Logic Malayalam Short/Reel published and logged.")


if __name__ == "__main__":
    main()