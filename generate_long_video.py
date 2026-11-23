#!/usr/bin/env python3
"""
Daily Logic Malayalam - Automated 8-10 Minute 16:9 Long-Form Video Pipeline
---------------------------------------------------------------------------
Schedule (3x/week synchronized rotation):
  - Tuesday  (7:30 PM IST build -> 8:30 PM IST publish): Everyday Myths Busted
  - Thursday (7:30 PM IST build -> 8:30 PM IST publish): How It Actually Works
  - Sunday   (7:30 PM IST build -> 8:30 PM IST publish): Why Is It Designed Like That?

Outputs:
  - 1920x1080 (16:9) MP4 with chapter title cards, landscape stock clips,
    Malayalam fact/myth-vs-fact callout overlays, and a 1280x720 custom thumbnail.
  - YouTube upload (Private at 7:30 PM IST with publishAt=8:30 PM IST) + Playlist + Previous Episode links.
  - Facebook Page Video upload at 8:30 PM IST (--publish-fb-only).
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import requests
from google import genai
from google.genai import types
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
WORK_DIR = Path("build_long")
HISTORY_FILE = Path("used_long_topics.json")
META_CACHE_FILE = WORK_DIR / "long_meta.json"
GRAPH_API_VERSION = "v21.0"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "").strip()
PRIVACY_STATUS = os.getenv("YT_PRIVACY_STATUS", "private").strip().lower()
TTS_VOICE = os.getenv("TTS_VOICE", "ml-IN-MidhunNeural").strip()
WATERMARK_TEXT = "Daily Logic Malayalam"

CATEGORY_META = {
    "myths_busted": {
        "label": "Everyday Myths Busted",
        "playlist": "Everyday Myths Busted (Malayalam)",
        "prompt_focus": (
            "5 to 6 widely believed everyday science, tech, vehicle, food, or home myths in Kerala/India "
            "and the real scientific facts that bust each myth."
        ),
    },
    "how_it_works": {
        "label": "How It Actually Works",
        "playlist": "How It Actually Works (Malayalam)",
        "prompt_focus": (
            "Step-by-step deep dive into how an everyday technology, appliance, vehicle system, or natural "
            "phenomenon actually works in real life (e.g., induction stoves, metro regenerative braking, "
            "fast phone chargers, inverter ACs, water purifiers)."
        ),
    },
    "why_designed": {
        "label": "Why Is It Designed Like That?",
        "playlist": "Why Is It Designed Like That? (Malayalam)",
        "prompt_focus": (
            "The hidden engineering and logic behind everyday designs we take for granted "
            "(e.g., why ceiling fans in India have 3 blades, why airplane windows are round, "
            "why railway tracks have stones, why manhole covers are round)."
        ),
    },
}

IST_TZ = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------------------
# Structured Output Schema
# ---------------------------------------------------------------------------
class ChapterPlan(BaseModel):
    chapter_title_ml: str = Field(description="Short Malayalam chapter title (3-6 words)")
    narration_ml: str = Field(
        description="180-230 words of natural, engaging, conversational Kerala Malayalam narration for this chapter."
    )
    key_fact_ml: str = Field(
        description="Crisp 1-2 line Malayalam takeaway fact (max 90 chars) for on-screen callout box."
    )
    myth_label_ml: Optional[str] = Field(
        default=None, description="Optional short myth statement in Malayalam (only for myths_busted)"
    )
    fact_label_ml: Optional[str] = Field(
        default=None, description="Optional short verified fact in Malayalam (only for myths_busted)"
    )
    search_queries: List[str] = Field(
        description="3 short English visual search queries (2-3 words each) for 16:9 stock clips matching this chapter."
    )


class LongVideoPlan(BaseModel):
    title: str = Field(description="Catchy Malayalam YouTube title (max 85 chars) with 1 relevant emoji")
    thumbnail_hook_ml: str = Field(description="Bold 3-5 word Malayalam curiosity dialogue/hook for 16:9 thumbnail")
    thumbnail_image_query: str = Field(description="2-3 concrete English words for the main subject photo on Pixabay (e.g. 'denim jeans pocket', 'airplane window').")
    intro_narration_ml: str = Field(
        description="70-90 words of engaging Malayalam intro hook previewing what viewers will learn."
    )
    chapters: List[ChapterPlan] = Field(description="Exactly 5 distinct chapters (~8-10 minutes total)")
    outro_narration_ml: str = Field(
        description=(
            "50-70 words summarizing the video and ending with: "
            "'ഇത്തരം കൗതുകകരവും ശാസ്ത്രീയവുമായ അറിവുകൾക്കായി ഡെയിലി ലോജിക് മലയാളം എന്ന നമ്മുടെ പേജും ചാനലും ഫോളോ ചെയ്യാനും സബ്സ്ക്രൈബ് ചെയ്യാനും മറക്കല്ലേ.'"
        )
    )
    description: str = Field(description="Detailed Malayalam YouTube description (chapters will be prepended)")
    tags: List[str] = Field(description="10-12 relevant Malayalam and English YouTube tags")


# ---------------------------------------------------------------------------
# Category & Topic History
# ---------------------------------------------------------------------------
def get_today_category() -> str:
    override = os.getenv("LONG_CATEGORY", "auto_by_day").strip().lower()
    if override in CATEGORY_META:
        return override
    # Python weekday: Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
    wd = datetime.now(IST_TZ).weekday()
    if wd == 1:
        return "myths_busted"
    if wd == 3:
        return "how_it_works"
    if wd == 6:
        return "why_designed"
    return "myths_busted"


def load_history() -> dict:
    if HISTORY_FILE.exists():
        try:
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"topics": [], "videos": []}


def save_history(topic: str, video_id: Optional[str], title: str, category: str) -> None:
    data = load_history()
    topics = data.get("topics", [])
    if topic not in topics:
        topics.append(topic)
    data["topics"] = topics[-120:]
    if video_id:
        videos = data.get("videos", [])
        videos.append(
            {
                "id": video_id,
                "title": title,
                "category": category,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "published_ist": datetime.now(IST_TZ).strftime("%Y-%m-%d %H:%M IST"),
            }
        )
        data["videos"] = videos[-50:]
    HISTORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Step 1: Grounded Topic Research & Fact-Checked Script Generation
# ---------------------------------------------------------------------------
def generate_fact_checked_plan(category: str) -> tuple[str, LongVideoPlan]:
    if not GEMINI_API_KEY:
        sys.exit("Error: GEMINI_API_KEY is not set.")
    client = genai.Client(api_key=GEMINI_API_KEY)
    history = load_history()
    used_topics = history.get("topics", [])
    cat_info = CATEGORY_META[category]

    models = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]

    # Pass 1: Web-grounded topic selection + verified factual notes
    research_prompt = f"""
You are a science & engineering researcher for the Malayalam YouTube channel "Daily Logic Malayalam".
Category for today: {cat_info['label']}
Focus: {cat_info['prompt_focus']}

STRICT RULES:
1. Pick ONE engaging, high-retention topic relatable to everyday life in Kerala/India (if trending in India recently, great, otherwise pick an evergreen curiosity topic).
2. Simple, relatable space/moon/planet topics are allowed ONLY if explained in plain, everyday Malayalam that any ordinary person enjoys—avoid heavy astrophysics jargon. Never give medical or financial advice.
3. NEVER repeat any of these previously covered topics: {used_topics[-40:]}
4. Use Google Search to verify every scientific fact, number, and mechanism so there are ZERO false claims.

Return a concise English research brief with:
- TOPIC_TITLE: <clear English topic title>
- VERIFIED_FACTS: <5 numbered bullet points with accurate, verified facts and numbers>
"""
    research_text = ""
    for m in models:
        try:
            resp = client.models.generate_content(
                model=m,
                contents=research_prompt,
                config=types.GenerateContentConfig(
                    tools=[{"google_search": {}}],
                    temperature=0.4,
                ),
            )
            if resp.text:
                research_text = resp.text.strip()
                break
        except Exception as exc:
            print(f"[WARN] Grounded research failed on {m}: {exc}")

    if not research_text:
        research_text = f"TOPIC_TITLE: {cat_info['label']} - Everyday Science & Engineering Explained"

    topic_match = re.search(r"TOPIC_TITLE:\s*(.+)", research_text)
    topic_title = topic_match.group(1).strip() if topic_match else research_text.splitlines()[0][:80]

    # Pass 2: Structured 8-10 minute Malayalam script
    script_prompt = f"""
Create an 8-to-10-minute Malayalam explainer video script for "Daily Logic Malayalam".
Category: {cat_info['label']}
Verified Research Brief:
{research_text}

REQUIREMENTS:
1. Total narration must be ~1,150 to 1,350 Malayalam words across intro + 5 chapters + outro so the video runs 8 to 10 minutes.
2. Write in natural, warm, conversational Kerala Malayalam (like a knowledgeable friend explaining clearly—not stiff textbook prose).
3. ONLY state verified facts from the research brief. Do NOT invent statistics, fake studies, or exaggerated claims.
4. Keep explanations simple, conversational, and enjoyable for everyday Malayalam listeners (even for space topics, avoid complex jargon).
5. For each of the 5 chapters, provide 3 short English stock-video search queries (e.g., "induction stove cooking", "ceiling fan spinning", "electric train station").
6. End outro_narration_ml with: "ഇത്തരം കൗതുകകരവും ശാസ്ത്രീയവുമായ അറിവുകൾക്കായി ഡെയിലി ലോജിക് മലയാളം എന്ന നമ്മുടെ പേജും ചാനലും ഫോളോ ചെയ്യാനും സബ്സ്ക്രൈബ് ചെയ്യാനും മറക്കല്ലേ."
"""
    plan: Optional[LongVideoPlan] = None
    for m in models:
        try:
            resp = client.models.generate_content(
                model=m,
                contents=script_prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=LongVideoPlan,
                    temperature=0.5,
                ),
            )
            plan = LongVideoPlan.model_validate_json(resp.text)
            break
        except Exception as exc:
            print(f"[WARN] Script generation failed on {m}: {exc}")

    if not plan:
        raise RuntimeError("Failed to generate LongVideoPlan with Gemini.")

    # Pass 3: Fact-audit verification pass
    audit_prompt = f"""
Audit this JSON Malayalam video script strictly for factual accuracy against standard physics, engineering, and science.
If any sentence contains an unverified statistic, false myth, or misleading claim, rewrite ONLY that sentence to be 100% factually accurate while keeping the same JSON structure and ~1,150-1,350 word length.
JSON:
{plan.model_dump_json()}
"""
    for m in models:
        try:
            resp = client.models.generate_content(
                model=m,
                contents=audit_prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=LongVideoPlan,
                    temperature=0.2,
                ),
            )
            plan = LongVideoPlan.model_validate_json(resp.text)
            print("[INFO] Fact-audit verification pass completed.")
            break
        except Exception as exc:
            print(f"[WARN] Fact-audit skipped on {m}: {exc}")

    return topic_title, plan


# ---------------------------------------------------------------------------
# Step 2: Fonts & Pillow 16:9 Card Renderers (HarfBuzz Malayalam Shaping)
# ---------------------------------------------------------------------------
def find_malayalam_font() -> str:
    candidates = [
        "/usr/share/fonts/truetype/smc/Manjari-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansMalayalam-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansMalayalamUI-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    return "DejaVuSans-Bold.ttf"


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> List[str]:
    words = text.split()
    lines, cur = [], ""
    for w in words:
        test = f"{cur} {w}".strip()
        if draw.textlength(test, font=font) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render_chapter_card(chapter_num: int, title_ml: str, out_png: Path, font_path: str) -> None:
    img = Image.new("RGB", (1920, 1080), (14, 24, 48))
    draw = ImageDraw.Draw(img)
    draw.rectangle([140, 400, 165, 680], fill=(250, 204, 21))
    f_label = ImageFont.truetype(font_path, 44)
    f_title = ImageFont.truetype(font_path, 72)
    draw.text((200, 410), f"ഭാഗം {chapter_num}", font=f_label, fill=(250, 204, 21))
    lines = wrap_text(draw, title_ml, f_title, 1520)[:2]
    y = 490
    for ln in lines:
        draw.text((200, y), ln, font=f_title, fill=(255, 255, 255))
        y += 95
    f_brand = ImageFont.truetype(font_path, 32)
    draw.text((200, 960), "ഡെയിലി ലോജിക് മലയാളം", font=f_brand, fill=(148, 163, 184))
    img.save(out_png)


def render_callout_overlay(chapter: ChapterPlan, out_png: Path, font_path: str) -> None:
    """Transparent 1920x1080 PNG overlay with wrapped Malayalam text that never overflows its box."""
    img = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    f_head = ImageFont.truetype(font_path, 34)
    f_body = ImageFont.truetype(font_path, 38)
    max_text_w = 1640

    if chapter.myth_label_ml and chapter.fact_label_ml:
        myth_lines = wrap_text(draw, f"മിഥ്യ: {chapter.myth_label_ml}", f_head, max_text_w)[:2]
        fact_lines = wrap_text(draw, f"സത്യം: {chapter.fact_label_ml}", f_body, max_text_w)[:2]
        total_lines = len(myth_lines) + len(fact_lines)
        box_h = 48 + total_lines * 52
        y0 = max(720, 1030 - box_h)
        y1 = 1030
        draw.rounded_rectangle([80, y0, 1840, y1], radius=24, fill=(10, 18, 36, 225), outline=(250, 204, 21, 235), width=4)
        cur_y = y0 + 24
        for ln in myth_lines:
            draw.text((120, cur_y), ln, font=f_head, fill=(248, 113, 113))
            cur_y += 48
        cur_y += 6
        for ln in fact_lines:
            draw.text((120, cur_y), ln, font=f_body, fill=(74, 222, 128))
            cur_y += 52
    else:
        fact_lines = wrap_text(draw, chapter.key_fact_ml, f_body, max_text_w)[:2]
        box_h = 84 + len(fact_lines) * 54
        y0 = max(750, 1030 - box_h)
        y1 = 1030
        draw.rounded_rectangle([80, y0, 1840, y1], radius=24, fill=(10, 18, 36, 225), outline=(250, 204, 21, 235), width=4)
        draw.text((120, y0 + 22), "പ്രധാന കാര്യം", font=f_head, fill=(250, 204, 21))
        cur_y = y0 + 72
        for ln in fact_lines:
            draw.text((120, cur_y), ln, font=f_body, fill=(255, 255, 255))
            cur_y += 54
    img.save(out_png)



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


def fetch_topic_image_from_pixabay(query: str, out_jpg: Path) -> bool:
    """Fetches a high-res topic-specific photo/illustration from Pixabay Images API."""
    if not PIXABAY_API_KEY or not query.strip():
        return False
    try:
        r = requests.get(
            "https://pixabay.com/api/",
            params={
                "key": PIXABAY_API_KEY,
                "q": query.strip(),
                "image_type": "all",
                "orientation": "horizontal",
                "per_page": 5,
                "safesearch": "true",
            },
            timeout=20,
        )
        hits = r.json().get("hits", [])
        for h in hits:
            url = h.get("largeImageURL") or h.get("webformatURL")
            if url:
                out_jpg.write_bytes(requests.get(url, timeout=30).content)
                return True
    except Exception as exc:
        print(f"[WARN] Pixabay image lookup failed for '{query}': {exc}")
    return False


def render_thumbnail_16x9(clip_path: Path, hook_ml: str, out_jpg: Path, font_path: str, thumb_query: str = "") -> None:
    """Creates a vibrant, Canva-style topic-specific 1280x720 thumbnail using a dedicated topic photo."""
    from PIL import ImageEnhance
    subject_jpg = WORK_DIR / "thumb_subject.jpg"
    got_topic_img = generate_gemini_topic_image(thumb_query, "16:9", subject_jpg) or fetch_topic_image_from_pixabay(thumb_query, subject_jpg)
    if not got_topic_img:
        subprocess.run(
            ["ffmpeg", "-y", "-ss", "1.5", "-i", str(clip_path), "-vframes", "1", str(subject_jpg)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    src_img = Image.open(subject_jpg).convert("RGB")
    src_img = ImageEnhance.Color(src_img).enhance(1.45)
    src_img = ImageEnhance.Contrast(src_img).enhance(1.22)

    W, H = 1280, 720
    scale = max(W / src_img.width, H / src_img.height)
    resized = src_img.resize((int(src_img.width * scale), int(src_img.height * scale)), Image.Resampling.LANCZOS)
    left = (resized.width - W) // 2
    top = (resized.height - H) // 2
    bg = resized.crop((left, top, left + W, top + H)).convert("RGBA")

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    for x in range(W):
        alpha = int(235 * max(0.25, 1.0 - (x / (W * 0.88))))
        odraw.line([(x, 0), (x, H)], fill=(12, 18, 46, alpha))
    odraw.rectangle([0, 440, W, H], fill=(10, 14, 38, 175))
    canvas = Image.alpha_composite(bg, overlay).convert("RGB")
    draw = ImageDraw.Draw(canvas)

    draw.rectangle([8, 8, W - 9, H - 9], outline=(255, 214, 0), width=12)

    f_badge = ImageFont.truetype(font_path, 38)
    draw.rounded_rectangle([48, 42, 470, 118], radius=36, fill=(239, 68, 68), outline=(255, 255, 255), width=4)
    draw.text((259, 80), "ഇതിന്റെ രഹസ്യം എന്ത്?", font=f_badge, fill=(255, 255, 255), anchor="mm")

    f_hook = ImageFont.truetype(font_path, 66)
    lines = wrap_text(draw, hook_ml, f_hook, 820)[:3]
    y = 220 if len(lines) == 3 else 270
    for ln in lines:
        draw.text((54, y + 5), ln, font=f_hook, fill=(0, 0, 0))
        draw.text((50, y), ln, font=f_hook, fill=(255, 235, 59))
        y += 92

    f_brand = ImageFont.truetype(font_path, 30)
    draw.rounded_rectangle([48, 616, 460, 672], radius=24, fill=(0, 180, 216))
    draw.text((254, 644), "ഡെയിലി ലോജിക് മലയാളം", font=f_brand, fill=(255, 255, 255), anchor="mm")

    canvas.save(out_jpg, quality=95)


def tts_to_mp3(text: str, out_mp3: Path) -> float:
    subprocess.run(
        [
            "edge-tts",
            "--voice",
            TTS_VOICE,
            "--rate=-4%",
            "--pitch=-2Hz",
            "--text",
            text,
            "--write-media",
            str(out_mp3),
        ],
        check=True,
    )
    probe = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(out_mp3),
        ],
        text=True,
    ).strip()
    return float(probe)


def fetch_landscape_clip(query: str, out_mp4: Path) -> bool:
    if not PIXABAY_API_KEY:
        return False
    try:
        r = requests.get(
            "https://pixabay.com/api/videos/",
            params={"key": PIXABAY_API_KEY, "q": query, "per_page": 5, "safesearch": "true"},
            timeout=20,
        )
        hits = r.json().get("hits", [])
        for h in hits:
            vids = h.get("videos", {})
            url = (vids.get("large") or vids.get("medium") or {}).get("url")
            if url:
                vid_data = requests.get(url, timeout=60).content
                out_mp4.write_bytes(vid_data)
                return True
    except Exception as exc:
        print(f"[WARN] Pixabay video lookup failed for '{query}': {exc}")

    photo_tmp = out_mp4.with_suffix(".jpg")
    if fetch_topic_image_from_pixabay(query, photo_tmp):
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loop", "1", "-i", str(photo_tmp),
                    "-vf", "scale=2160:1215:force_original_aspect_ratio=increase,crop=2160:1215,zoompan=z='min(zoom+0.0012,1.15)':d=240:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1920x1080,fps=30,format=yuv420p",
                    "-t", "8", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", str(out_mp4),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception as exc:
            print(f"[WARN] Ken Burns photo fallback failed for '{query}': {exc}")
    return False


def build_long_video(plan: LongVideoPlan) -> tuple[Path, Path, str]:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    font_path = find_malayalam_font()
    segments = []
    chapter_timestamps = []
    elapsed = 0.0

    # Intro segment
    intro_mp3 = WORK_DIR / "intro.mp3"
    intro_dur = tts_to_mp3(plan.intro_narration_ml, intro_mp3)
    intro_clip = WORK_DIR / "intro_stock.mp4"
    if not fetch_landscape_clip(plan.chapters[0].search_queries[0], intro_clip):
        raise RuntimeError("Could not download intro stock video from Pixabay.")
    intro_seg = WORK_DIR / "seg_intro.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-stream_loop", "-1", "-i", str(intro_clip), "-i", str(intro_mp3),
            "-t", f"{intro_dur:.2f}", "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps=30",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-ac", "2", "-ar", "44100", "-b:a", "192k", "-shortest", str(intro_seg),
        ],
        check=True,
    )
    segments.append(intro_seg)
    elapsed += intro_dur

    # 5 Chapters
    first_clip_for_thumb = intro_clip
    for idx, ch in enumerate(plan.chapters, start=1):
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        chapter_timestamps.append(f"{mins:02d}:{secs:02d} {ch.chapter_title_ml}")

        # 2.5s Chapter title card
        card_png = WORK_DIR / f"card_{idx}.png"
        card_mp4 = WORK_DIR / f"seg_card_{idx}.mp4"
        render_chapter_card(idx, ch.chapter_title_ml, card_png, font_path)
        subprocess.run(
            [
                "ffmpeg", "-y", "-loop", "1", "-i", str(card_png),
                "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-t", "2.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
                "-c:a", "aac", "-ac", "2", "-ar", "44100", "-b:a", "192k", "-shortest", str(card_mp4),
            ],
            check=True,
        )
        segments.append(card_mp4)
        elapsed += 2.5

        # Chapter narration + 3 landscape clips concatenated + callout overlay
        ch_mp3 = WORK_DIR / f"ch_{idx}.mp3"
        ch_dur = tts_to_mp3(ch.narration_ml, ch_mp3)
        ch_clips = []
        for q_i, q in enumerate(ch.search_queries[:3]):
            c_path = WORK_DIR / f"ch_{idx}_clip_{q_i}.mp4"
            if fetch_landscape_clip(q, c_path):
                ch_clips.append(c_path)
        if not ch_clips:
            ch_clips = [intro_clip]

        per_clip_dur = ch_dur / len(ch_clips)
        trimmed_clips = []
        for c_i, c_path in enumerate(ch_clips):
            tr_path = WORK_DIR / f"ch_{idx}_trim_{c_i}.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-stream_loop", "-1", "-i", str(c_path),
                    "-t", f"{per_clip_dur:.2f}",
                    "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps=30",
                    "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "23",
                    str(tr_path),
                ],
                check=True,
            )
            trimmed_clips.append(tr_path)

        concat_txt = WORK_DIR / f"ch_{idx}_concat.txt"
        concat_txt.write_text("\n".join(f"file '{p.name}'" for p in trimmed_clips), encoding="utf-8")
        ch_raw_vid = WORK_DIR / f"ch_{idx}_raw.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_txt), "-c", "copy", str(ch_raw_vid)],
            check=True,
        )

        overlay_png = WORK_DIR / f"overlay_{idx}.png"
        render_callout_overlay(ch, overlay_png, font_path)
        ch_seg = WORK_DIR / f"seg_ch_{idx}.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(ch_raw_vid), "-i", str(ch_mp3), "-i", str(overlay_png),
                "-filter_complex", "[0:v][2:v]overlay=0:0[vout]",
                "-map", "[vout]", "-map", "1:a:0",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-ac", "2", "-ar", "44100", "-b:a", "192k", "-shortest", str(ch_seg),
            ],
            check=True,
        )
        segments.append(ch_seg)
        elapsed += ch_dur

    # Outro segment
    outro_mp3 = WORK_DIR / "outro.mp3"
    outro_dur = tts_to_mp3(plan.outro_narration_ml, outro_mp3)
    outro_seg = WORK_DIR / "seg_outro.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-stream_loop", "-1", "-i", str(intro_clip), "-i", str(outro_mp3),
            "-t", f"{outro_dur:.2f}", "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps=30",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-ac", "2", "-ar", "44100", "-b:a", "192k", "-shortest", str(outro_seg),
        ],
        check=True,
    )
    segments.append(outro_seg)

    # Concatenate all segments with +faststart
    full_concat = WORK_DIR / "full_concat.txt"
    full_concat.write_text("\n".join(f"file '{p.name}'" for p in segments), encoding="utf-8")
    final_mp4 = WORK_DIR / "long_video.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(full_concat),
            "-c:v", "copy", "-c:a", "aac", "-ac", "2", "-ar", "44100", "-b:a", "192k",
            "-movflags", "+faststart", str(final_mp4),
        ],
        check=True,
    )

    thumb_jpg = WORK_DIR / "long_thumb.jpg"
    render_thumbnail_16x9(first_clip_for_thumb, plan.thumbnail_hook_ml, thumb_jpg, font_path, plan.thumbnail_image_query)
    chapters_text = "Chapters:\n00:00 Intro\n" + "\n".join(chapter_timestamps)
    return final_mp4, thumb_jpg, chapters_text


# ---------------------------------------------------------------------------
# Step 4: YouTube Upload (8:30 PM IST publishAt) + Playlist + Previous Links
# ---------------------------------------------------------------------------
def get_youtube_service():
    creds = Credentials(
        token=None,
        refresh_token=os.getenv("YT_REFRESH_TOKEN", "").strip(),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.getenv("YT_CLIENT_ID", "").strip(),
        client_secret=os.getenv("YT_CLIENT_SECRET", "").strip(),
    )
    creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)


def add_to_playlist_safe(youtube, video_id: str, playlist_title: str) -> None:
    try:
        pls = youtube.playlists().list(part="snippet", mine=True, maxResults=50).execute()
        pl_id = None
        for item in pls.get("items", []):
            if item["snippet"]["title"].strip().lower() == playlist_title.strip().lower():
                pl_id = item["id"]
                break
        if not pl_id:
            created = youtube.playlists().insert(
                part="snippet,status",
                body={
                    "snippet": {"title": playlist_title, "description": f"{playlist_title} - Daily Logic Malayalam"},
                    "status": {"privacyStatus": "public"},
                },
            ).execute()
            pl_id = created["id"]
        youtube.playlistItems().insert(
            part="snippet",
            body={"snippet": {"playlistId": pl_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}},
        ).execute()
        print(f"[SUCCESS] Added video to YouTube playlist: {playlist_title}")
    except Exception as exc:
        print(f"[INFO] Playlist update skipped (requires youtube or youtube.force-ssl OAuth scope): {exc}")


def upload_long_to_youtube(video_path: Path, thumb_jpg: Path, plan: LongVideoPlan, chapters_text: str, category: str) -> str:
    youtube = get_youtube_service()
    history = load_history()
    prev_videos = history.get("videos", [])[-3:]
    prev_links_block = ""
    if prev_videos:
        prev_links_block = "\n\nWatch Previous Episodes:\n" + "\n".join(
            f"- {v['title']}: {v['url']}" for v in reversed(prev_videos)
        )

    full_description = f"{chapters_text}\n\n{plan.description}{prev_links_block}".strip()

    status_obj = {
        "privacyStatus": PRIVACY_STATUS,
        "selfDeclaredMadeForKids": False,
        "containsSyntheticMedia": False,
    }
    if PRIVACY_STATUS == "private":
        ist_now = datetime.now(IST_TZ)
        target_ist = ist_now.replace(hour=20, minute=30, second=0, microsecond=0)
        if ist_now >= target_ist - timedelta(minutes=10):
            target_ist = ist_now + timedelta(minutes=60)
        status_obj["publishAt"] = target_ist.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"[INFO] Long video scheduled for YouTube auto-publish at {status_obj['publishAt']} (8:30 PM IST).")

    request = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": plan.title[:100],
                "description": full_description[:4900],
                "tags": plan.tags,
                "categoryId": "28",
                "defaultLanguage": "ml",
                "defaultAudioLanguage": "ml",
            },
            "status": status_obj,
        },
        media_body=MediaFileUpload(str(video_path), mimetype="video/mp4", chunksize=1024 * 1024 * 5, resumable=True),
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"[INFO] YouTube upload progress: {int(status.progress() * 100)}%")

    video_id = response["id"]
    print(f"[SUCCESS] Uploaded Long Video! URL: https://www.youtube.com/watch?v={video_id}")

    # Wait for YouTube processing to finish + 10s cooling period before setting custom thumbnail
    for _ in range(20):
        try:
            vinfo = youtube.videos().list(part="processingDetails", id=video_id).execute()
            pstat = (
                vinfo.get("items", [{}])[0]
                .get("processingDetails", {})
                .get("processingStatus", "")
            )
            print(f"[INFO] YouTube processingStatus: {pstat or 'pending'}")
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
            videoId=video_id, media_body=MediaFileUpload(str(thumb_jpg), mimetype="image/jpeg")
        ).execute()
        print("[SUCCESS] 16:9 Custom Thumbnail uploaded to YouTube!")
    except Exception as exc:
        print(f"[WARN] Thumbnail upload failed: {exc}")

    add_to_playlist_safe(youtube, video_id, CATEGORY_META[category]["playlist"])
    return video_id


# ---------------------------------------------------------------------------
# Step 5: Facebook Page Video Upload (8:30 PM IST)
# ---------------------------------------------------------------------------
def upload_long_to_facebook(video_path: Path, title: str, description: str) -> None:
    access_token = os.getenv("META_ACCESS_TOKEN", "").strip()
    fb_page_id = os.getenv("FB_PAGE_ID", "").strip()
    if not access_token or not fb_page_id:
        print("[SKIP] Facebook Video skipped (META_ACCESS_TOKEN or FB_PAGE_ID not set).")
        return

    try:
        pt_resp = requests.get(
            f"https://graph.facebook.com/{GRAPH_API_VERSION}/{fb_page_id}",
            params={"fields": "access_token", "access_token": access_token},
            timeout=20,
        )
        if pt_resp.ok and pt_resp.json().get("access_token"):
            access_token = pt_resp.json()["access_token"]
    except Exception:
        pass

    url = f"https://graph-video.facebook.com/{GRAPH_API_VERSION}/{fb_page_id}/videos"
    file_size = video_path.stat().st_size
    print(f"[INFO] Starting Facebook chunked upload ({file_size / (1024 * 1024):.1f} MB)...")

    # Phase 1: Start chunked upload session
    start_resp = requests.post(
        url,
        data={"upload_phase": "start", "file_size": str(file_size), "access_token": access_token},
        timeout=30,
    )
    if not start_resp.ok:
        print(f"[ERROR] Facebook start HTTP {start_resp.status_code}: {start_resp.text}")
        start_resp.raise_for_status()
    sdata = start_resp.json()
    session_id = sdata["upload_session_id"]
    video_id = sdata["video_id"]
    start_offset = int(sdata["start_offset"])
    end_offset = int(sdata["end_offset"])

    # Phase 2: Transfer chunks
    with open(video_path, "rb") as f:
        while start_offset < file_size:
            f.seek(start_offset)
            chunk_len = max(1, end_offset - start_offset)
            chunk = f.read(chunk_len)
            if not chunk:
                break
            tr_resp = requests.post(
                url,
                data={
                    "upload_phase": "transfer",
                    "upload_session_id": session_id,
                    "start_offset": str(start_offset),
                    "access_token": access_token,
                },
                files={"video_file_chunk": ("chunk.mp4", chunk, "application/octet-stream")},
                timeout=180,
            )
            if not tr_resp.ok:
                print(f"[ERROR] Facebook transfer HTTP {tr_resp.status_code}: {tr_resp.text}")
                tr_resp.raise_for_status()
            tdata = tr_resp.json()
            start_offset = int(tdata["start_offset"])
            end_offset = int(tdata["end_offset"])
            print(f"[INFO] Facebook upload progress: {min(100, int(start_offset * 100 / file_size))}%")

    # Phase 3: Finish + attach separate 16:9 thumbnail if available
    finish_data = {
        "upload_phase": "finish",
        "upload_session_id": session_id,
        "title": title[:100],
        "description": description[:4000],
        "access_token": access_token,
    }
    thumb_path = WORK_DIR / "long_thumb.jpg"
    if thumb_path.exists():
        with open(thumb_path, "rb") as tf:
            fin_resp = requests.post(
                url,
                data=finish_data,
                files={"thumb": ("long_thumb.jpg", tf, "image/jpeg")},
                timeout=60,
            )
    else:
        fin_resp = requests.post(url, data=finish_data, timeout=60)

    if not fin_resp.ok:
        print(f"[ERROR] Facebook finish HTTP {fin_resp.status_code}: {fin_resp.text}")
        fin_resp.raise_for_status()
    print(f"[SUCCESS] Published Long Video + Thumbnail to Facebook Page! Video ID: {video_id}")


def main() -> None:
    if "--publish-fb-only" in sys.argv:
        vid = WORK_DIR / "long_video.mp4"
        if not vid.exists() or not META_CACHE_FILE.exists():
            print("[SKIP] No cached long video found for 8:30 PM Facebook publish.")
            return
        meta = json.loads(META_CACHE_FILE.read_text(encoding="utf-8"))
        upload_long_to_facebook(vid, meta["title"], meta["description"])
        return

    category = get_today_category()
    print(f"[1/4] Generating fact-checked 8-10 min Malayalam plan for: {CATEGORY_META[category]['label']}...")
    topic_title, plan = generate_fact_checked_plan(category)
    print(f"      Topic: {topic_title}")

    print("[2/4] Rendering 1920x1080 video with chapters & callouts...")
    video_path, thumb_jpg, chapters_text = build_long_video(plan)

    META_CACHE_FILE.write_text(
        json.dumps(
            {"title": plan.title, "description": f"{chapters_text}\n\n{plan.description}"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print("[3/4] Uploading to YouTube (Private -> 8:30 PM IST Auto-Publish)...")
    video_id = upload_long_to_youtube(video_path, thumb_jpg, plan, chapters_text, category)

    print("[4/4] Saving topic & episode history...")
    save_history(topic_title, video_id, plan.title, category)
    print("[DONE] 7:30 PM long video build finished.")


if __name__ == "__main__":
    main()