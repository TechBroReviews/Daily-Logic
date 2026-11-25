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
import math
import struct
import time
import urllib.parse
import wave
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
    animation_scene: str = Field(
        description="Best matching 2D animation type from: 'wheel_brake', 'sound_waves', 'battery_heat', 'pressure_flow', 'gear_mechanism', 'light_optics', 'heart_pulse', 'comparison_bars'."
    )
    search_queries: List[str] = Field(
        description="3 literal English visual search queries (2-3 words each, NO abbreviations like ABS/LED/AC) for 16:9 stock clips."
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


def save_history(topic: str, video_id: Optional[str], title: str, category: str, chapter_titles: Optional[List[str]] = None) -> None:
    data = load_history()
    topics = data.get("topics", [])
    if topic not in topics:
        topics.append(topic)
    data["topics"] = topics[-120:]
    if chapter_titles:
        chapters = data.get("chapters", [])
        for ct in chapter_titles:
            if ct and ct not in chapters:
                chapters.append(ct)
        data["chapters"] = chapters[-250:]
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


def find_latin_font() -> str:
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        if Path(path).exists():
            return path
    return "DejaVuSans-Bold.ttf"


def _split_script_runs(text: str) -> List[tuple[str, bool]]:
    return [(part, bool(re.fullmatch(r"[A-Za-z]+", part))) for part in re.findall(r"[A-Za-z]+|[^A-Za-z]+", text)]


def measure_mixed_width(draw: ImageDraw.ImageDraw, text: str, ml_font: ImageFont.FreeTypeFont, lat_font: ImageFont.FreeTypeFont) -> float:
    return sum(draw.textlength(seg, font=lat_font if is_lat else ml_font) for seg, is_lat in _split_script_runs(text))


def wrap_mixed_text_px(
    draw: ImageDraw.ImageDraw, text: str, ml_font: ImageFont.FreeTypeFont, lat_font: ImageFont.FreeTypeFont, max_w: int
) -> List[str]:
    words = text.split()
    lines, cur = [], ""
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


def draw_mixed_text_at(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    text: str,
    ml_font: ImageFont.FreeTypeFont,
    lat_font: ImageFont.FreeTypeFont,
    fill: tuple,
    anchor_y: str = "t",
) -> None:
    cur_x = x
    for seg, is_lat in _split_script_runs(text):
        fnt = lat_font if is_lat else ml_font
        draw.text((cur_x, y), seg, font=fnt, fill=fill, anchor=f"l{anchor_y}")
        cur_x += draw.textlength(seg, font=fnt)


def synthesize_looping_bgm_60s(out_wav: Path, duration_sec: float = 60.0, sample_rate: int = 44100) -> None:
    total_samples = int(duration_sec * sample_rate)
    chords = [
        (261.63, 329.63, 392.00, 493.88),
        (220.00, 261.63, 329.63, 392.00),
        (174.61, 220.00, 261.63, 329.63),
        (196.00, 246.94, 293.66, 329.63),
    ]
    chord_len = 5.0
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
                env = math.sin(math.pi * min(1.0, local_t / chord_len)) ** 0.45
                freqs = chords[chord_idx]
                val = sum(math.sin(2.0 * math.pi * f * t) * (0.28 if idx_f == 0 else 0.17) for idx_f, f in enumerate(freqs))
                pcm = int(max(-1.0, min(1.0, val * env * 0.32)) * 32767)
                buf.extend(struct.pack("<hh", pcm, pcm))
            wf.writeframes(bytes(buf))


def synthesize_whoosh_wav(out_wav: Path, duration_sec: float = 0.45, sample_rate: int = 44100) -> None:
    import random as _rnd
    rng = _rnd.Random(42)
    total_samples = int(duration_sec * sample_rate)
    prev = 0.0
    with wave.open(str(out_wav), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        buf = bytearray()
        for i in range(total_samples):
            t = i / total_samples
            env = math.sin(math.pi * t) ** 1.5
            alpha = 0.08 + 0.25 * math.sin(math.pi * t)
            prev = prev + alpha * (rng.uniform(-1.0, 1.0) - prev)
            pcm = int(max(-1.0, min(1.0, prev * env * 0.50)) * 32767)
            buf.extend(struct.pack("<hh", pcm, pcm))
        wf.writeframes(bytes(buf))


def render_2d_animation_16x9(scene_type: str, title_ml: str, duration_sec: float, out_mp4: Path, font_path: str) -> None:
    """Renders a 1920x1080 30fps 2D vector animation clip in the upper/center stage (y=60..760), cleanly above the callout box."""
    fps = 30
    total_frames = max(60, int(round(duration_sec * fps)))
    W, H = 1920, 1080
    lat_path = find_latin_font()
    bg = Image.new("RGB", (W, H), (10, 17, 36))
    bg_d = ImageDraw.Draw(bg)
    for gx in range(0, W, 120):
        bg_d.line([(gx, 0), (gx, H)], fill=(18, 32, 62), width=2)
    for gy in range(0, H, 120):
        bg_d.line([(0, gy), (W, gy)], fill=(18, 32, 62), width=2)

    ml_f = ImageFont.truetype(font_path, 40)
    lat_f = ImageFont.truetype(lat_path, 40)
    title_lines = wrap_mixed_text_px(bg_d, title_ml, ml_f, lat_f, 1500)[:1]

    cmd = [
        "ffmpeg", "-y", "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{W}x{H}", "-pix_fmt", "rgb24", "-r", str(fps), "-i", "-",
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", str(out_mp4),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert proc.stdin is not None
    cx, cy = 960, 445
    try:
        for f in range(total_frames):
            t = f / fps
            progress = f / max(1, total_frames - 1)
            frame = bg.copy()
            d = ImageDraw.Draw(frame)
            d.rounded_rectangle([110, 60, 1810, 770], radius=32, fill=(13, 23, 46), outline=(0, 229, 255), width=4)
            tw = measure_mixed_width(d, title_lines[0], ml_f, lat_f)
            draw_mixed_text_at(d, 960 - tw / 2, 112, title_lines[0], ml_f, lat_f, (250, 204, 21), anchor_y="m")

            if scene_type == "wheel_brake":
                d.line([(260, 660), (1660, 660)], fill=(0, 229, 255), width=6)
                r_out, r_rim, r_disc = 195, 130, 88
                d.ellipse([cx - r_out, cy - r_out, cx + r_out, cy + r_out], fill=(24, 30, 44), outline=(148, 163, 184), width=10)
                d.ellipse([cx - r_rim, cy - r_rim, cx + r_rim, cy + r_rim], fill=(38, 52, 82), outline=(203, 213, 225), width=5)
                for sp in range(6):
                    a = t * 4.5 + sp * (math.pi / 3.0)
                    d.line([(cx, cy), (cx + int(r_rim * math.cos(a)), cy + int(r_rim * math.sin(a)))], fill=(148, 163, 184), width=8)
                d.ellipse([cx - r_disc, cy - r_disc, cx + r_disc, cy + r_disc], fill=(71, 85, 105), outline=(226, 232, 240), width=4)
                pulse_on = int(t * 7.0) % 2 == 0
                cal_col = (239, 68, 68) if pulse_on else (250, 204, 21)
                off = 0 if pulse_on else 14
                d.rounded_rectangle([cx + 65 + off, cy - 70, cx + 135 + off, cy + 70], radius=16, fill=cal_col, outline=(255, 255, 255), width=4)
            elif scene_type == "sound_waves":
                pts1, pts2 = [], []
                for x in range(240, 1680, 18):
                    ph = (x - 240) * 0.02 - t * 6.0
                    pts1.append((x, cy - 80 + int(65 * math.sin(ph))))
                    pts2.append((x, cy + 90 + int(65 * math.sin(ph + math.pi * min(1.0, progress * 1.3)))))
                d.line(pts1, fill=(248, 113, 113), width=6)
                d.line(pts2, fill=(0, 229, 255), width=6)
            elif scene_type == "battery_heat":
                d.rounded_rectangle([720, 220, 1200, 670], radius=30, fill=(18, 30, 58), outline=(0, 229, 255), width=6)
                d.rounded_rectangle([880, 185, 1040, 225], radius=10, fill=(0, 229, 255))
                bars = int(1 + 4 * ((t * 0.8) % 1.0))
                for b in range(4):
                    by0 = 560 - b * 100
                    if b < bars:
                        d.rounded_rectangle([760, by0, 1160, by0 + 80], radius=14, fill=(239, 68, 68) if b == 3 else (74, 222, 128))
            elif scene_type == "pressure_flow":
                d.rounded_rectangle([680, 310, 1240, 670], radius=32, fill=(20, 36, 68), outline=(56, 189, 248), width=6)
                d.rectangle([890, 240, 1030, 315], fill=(250, 204, 21))
                for p_i in range(16):
                    px = 740 + ((p_i * 63) % 440)
                    py = 640 - int(((t * 220 + p_i * 55) % 400))
                    d.ellipse([px - 10, py - 10, px + 10, py + 10], fill=(0, 229, 255) if py > 310 else (255, 255, 255))
            elif scene_type == "light_optics":
                d.polygon([(960, 240), (760, 620), (1160, 620)], fill=(22, 42, 82), outline=(0, 229, 255), width=6)
                d.line([(260, 400), (860, 440)], fill=(255, 255, 255), width=10)
                for idx_r, rc in enumerate([(239, 68, 68), (250, 204, 21), (74, 222, 128), (56, 189, 248)]):
                    d.line([(1060, 440), (1660, 320 + idx_r * 80 + int(18 * math.sin(t * 4.0 + idx_r)))], fill=rc, width=8)
            elif scene_type == "heart_pulse":
                pts = []
                for x in range(240, 1680, 14):
                    pos = ((x + int(t * 320)) % 240) / 240.0
                    dy = -160 if 0.40 < pos < 0.48 else (105 if 0.48 <= pos < 0.56 else 0)
                    pts.append((x, cy + dy))
                d.line(pts, fill=(74, 222, 128), width=8)
            elif scene_type == "comparison_bars":
                for b_i, (bc, tgt) in enumerate(zip([(248, 113, 113), (250, 204, 21), (74, 222, 128)], [0.45, 0.72, 0.95])):
                    bx0 = 560 + b_i * 310
                    bh = int(380 * tgt * min(1.0, progress * 1.4))
                    d.rounded_rectangle([bx0, 650 - bh, bx0 + 160, 650], radius=16, fill=bc)
            else:
                for g_cx, g_cy, rad, teeth, dir_s, col in [(820, 420, 135, 10, 1.0, (0, 229, 255)), (1080, 510, 105, 8, -1.25, (250, 204, 21))]:
                    d.ellipse([g_cx - rad, g_cy - rad, g_cx + rad, g_cy + rad], outline=col, width=10)
                    d.ellipse([g_cx - 34, g_cy - 34, g_cx + 34, g_cy + 34], fill=col)
                    for k in range(teeth):
                        ang = dir_s * t * 2.2 + k * (2 * math.pi / teeth)
                        d.line([
                            (g_cx + int((rad - 8) * math.cos(ang)), g_cy + int((rad - 8) * math.sin(ang))),
                            (g_cx + int((rad + 24) * math.cos(ang)), g_cy + int((rad + 24) * math.sin(ang))),
                        ], fill=col, width=16)

            d.rounded_rectangle([240, 720, 1680, 738], radius=9, fill=(30, 46, 80))
            d.rounded_rectangle([240, 720, 240 + max(20, int(1440 * progress)), 738], radius=9, fill=(0, 229, 255))
            proc.stdin.write(frame.tobytes())
    finally:
        proc.stdin.close()
        if proc.wait() != 0:
            raise RuntimeError("FFmpeg 2D 16:9 render failed")


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
    lat_path = find_latin_font()
    draw.rectangle([140, 400, 165, 680], fill=(250, 204, 21))
    f_label = ImageFont.truetype(font_path, 44)
    f_title_ml = ImageFont.truetype(font_path, 68)
    f_title_lat = ImageFont.truetype(lat_path, 68)
    draw.text((200, 410), f"ഭാഗം {chapter_num}", font=f_label, fill=(250, 204, 21))
    lines = wrap_mixed_text_px(draw, title_ml, f_title_ml, f_title_lat, 1520)[:2]
    y = 490
    for ln in lines:
        draw_mixed_text_at(draw, 200, y, ln, f_title_ml, f_title_lat, (255, 255, 255))
        y += 92
    f_brand = ImageFont.truetype(font_path, 32)
    draw.text((200, 960), "ഡെയിലി ലോജിക് മലയാളം", font=f_brand, fill=(148, 163, 184))
    img.save(out_png)


def render_callout_overlay(chapter: ChapterPlan, out_png: Path, font_path: str) -> None:
    """Transparent 1920x1080 PNG overlay with mixed-script rendering and zero box overflow."""
    img = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    lat_path = find_latin_font()
    f_head_ml = ImageFont.truetype(font_path, 32)
    f_head_lat = ImageFont.truetype(lat_path, 32)
    f_body_ml = ImageFont.truetype(font_path, 36)
    f_body_lat = ImageFont.truetype(lat_path, 36)
    max_text_w = 1640

    if chapter.myth_label_ml and chapter.fact_label_ml:
        myth_lines = wrap_mixed_text_px(draw, f"മിഥ്യ: {chapter.myth_label_ml}", f_head_ml, f_head_lat, max_text_w)[:2]
        fact_lines = wrap_mixed_text_px(draw, f"സത്യം: {chapter.fact_label_ml}", f_body_ml, f_body_lat, max_text_w)[:2]
        total_lines = len(myth_lines) + len(fact_lines)
        box_h = 48 + total_lines * 50
        y0 = max(760, 1030 - box_h)
        y1 = 1030
        draw.rounded_rectangle([80, y0, 1840, y1], radius=24, fill=(10, 18, 36, 225), outline=(250, 204, 21, 235), width=4)
        cur_y = y0 + 24
        for ln in myth_lines:
            draw_mixed_text_at(draw, 120, cur_y, ln, f_head_ml, f_head_lat, (248, 113, 113))
            cur_y += 46
        cur_y += 6
        for ln in fact_lines:
            draw_mixed_text_at(draw, 120, cur_y, ln, f_body_ml, f_body_lat, (74, 222, 128))
            cur_y += 50
    else:
        fact_lines = wrap_mixed_text_px(draw, chapter.key_fact_ml, f_body_ml, f_body_lat, max_text_w)[:2]
        box_h = 84 + len(fact_lines) * 52
        y0 = max(780, 1030 - box_h)
        y1 = 1030
        draw.rounded_rectangle([80, y0, 1840, y1], radius=24, fill=(10, 18, 36, 225), outline=(250, 204, 21, 235), width=4)
        draw.text((120, y0 + 22), "പ്രധാന കാര്യം", font=f_head_ml, fill=(250, 204, 21))
        cur_y = y0 + 70
        for ln in fact_lines:
            draw_mixed_text_at(draw, 120, cur_y, ln, f_body_ml, f_body_lat, (255, 255, 255))
            cur_y += 52
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

    lat_path = find_latin_font()
    f_hook_ml = ImageFont.truetype(font_path, 62)
    f_hook_lat = ImageFont.truetype(lat_path, 62)
    lines = wrap_mixed_text_px(draw, hook_ml, f_hook_ml, f_hook_lat, 800)[:3]
    y = 220 if len(lines) == 3 else 270
    for ln in lines:
        draw_mixed_text_at(draw, 54, y + 5, ln, f_hook_ml, f_hook_lat, (0, 0, 0))
        draw_mixed_text_at(draw, 50, y, ln, f_hook_ml, f_hook_lat, (255, 235, 59))
        y += 88

    f_brand = ImageFont.truetype(font_path, 30)
    draw.rounded_rectangle([48, 616, 460, 672], radius=24, fill=(0, 180, 216))
    draw.text((254, 644), "ഡെയിലി ലോജിക് മലയാളം", font=f_brand, fill=(255, 255, 255), anchor="mm")

    canvas.save(out_jpg, quality=95)



def _srt_ts_to_sec(ts: str) -> float:
    ts = ts.strip().replace(",", ".")
    parts = ts.split(":")
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])


def _sec_to_ass_ts(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def srt_to_karaoke_ass(srt_path: Path, ass_path: Path) -> None:
    """Converts edge-tts SRT into 1080p bottom ASS Karaoke subtitles with yellow word sweep."""
    raw = srt_path.read_text(encoding="utf-8", errors="ignore").strip() if srt_path.exists() else ""
    cues: List[Tuple[float, float, str]] = []
    for block in re.split(r"\r?\n\s*\r?\n", raw):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if len(lines) >= 3 and "-->" in lines[1]:
            s_t, e_t = [x.strip() for x in lines[1].split("-->")[:2]]
            cues.append((_srt_ts_to_sec(s_t), _srt_ts_to_sec(e_t), " ".join(lines[2:])))
    # Merge short word cues into readable 5-7 word phrases
    merged: List[Tuple[float, float, str]] = []
    buf_w: List[str] = []
    b_s, b_e = 0.0, 0.0
    for st, en, txt in cues:
        words = txt.split()
        if not buf_w:
            b_s = st
        if len(buf_w) + len(words) > 6:
            merged.append((b_s, b_e, " ".join(buf_w)))
            buf_w = list(words)
            b_s, b_e = st, en
        else:
            buf_w.extend(words)
            b_e = en
    if buf_w:
        merged.append((b_s, b_e, " ".join(buf_w)))

    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Karaoke,Noto Sans Malayalam,50,&H0015CCFA,&H00FFFFFF,&H0022100A,&HD822100A,-1,0,0,0,100,100,0,0,3,12,0,2,120,120,52,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev_lines = []
    for st, en, txt in merged:
        dur_cs = max(20, int(round((en - st) * 100)))
        words = txt.split()
        if not words:
            continue
        per_cs = max(8, dur_cs // len(words))
        k_txt = " ".join(f"{{\\kf{per_cs}}}{w}" for w in words)
        ev_lines.append(f"Dialogue: 0,{_sec_to_ass_ts(st)},{_sec_to_ass_ts(en)},Karaoke,,0,0,0,,{k_txt}")
    ass_path.write_text(header + "\n".join(ev_lines) + "\n", encoding="utf-8")


def tts_to_mp3(text: str, out_mp3: Path, out_srt: Optional[Path] = None) -> float:
    cmd = [
        "edge-tts",
        "--voice",
        TTS_VOICE,
        "--rate=-4%",
        "--pitch=-2Hz",
        "--text",
        text,
        "--write-media",
        str(out_mp3),
    ]
    if out_srt is not None:
        cmd.extend(["--write-subtitles", str(out_srt)])
    subprocess.run(cmd, check=True)
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


def search_web_topic_photo_16x9(query: str, out_jpg: Path) -> bool:
    if not query.strip():
        return False
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        r = requests.get(
            f"https://www.bing.com/images/search?q={urllib.parse.quote_plus(query)}&qft=+filterui:imagesize-large&form=HDRSC2",
            headers=headers,
            timeout=15,
        )
        if r.ok:
            for img_url in re.findall(r'murl(?:&quot;|")\s*:\s*(?:&quot;|")(https?://[^"&]+)', r.text)[:8]:
                if any(b in img_url.lower() for b in ("logo", "favicon", "icon", ".svg")):
                    continue
                try:
                    ir = requests.get(img_url, headers=headers, timeout=15)
                    if ir.ok and len(ir.content) > 18000:
                        out_jpg.write_bytes(ir.content)
                        with Image.open(out_jpg) as im:
                            if im.width >= 450 and im.height >= 250:
                                im.convert("RGB").save(out_jpg, quality=92)
                                return True
                except Exception:
                    continue
    except Exception:
        pass
    return False


def render_dynamic_topic_16x9(img_path: Path, out_mp4: Path, duration_sec: float = 6.0, style_idx: int = 0) -> None:
    """Renders 1920x1080 30fps dynamic fly-in + zoom + swipe-out animation for topic photos."""
    from PIL import ImageEnhance
    fps = 30
    total_frames = max(60, int(round(duration_sec * fps)))
    W, H = 1920, 1080
    cx, cy = 960, 445
    bg = Image.new("RGB", (W, H), (10, 17, 36))
    bg_d = ImageDraw.Draw(bg)
    for gx in range(0, W, 120):
        bg_d.line([(gx, 0), (gx, H)], fill=(18, 32, 62), width=2)
    for gy in range(0, H, 120):
        bg_d.line([(0, gy), (W, gy)], fill=(18, 32, 62), width=2)

    prod = ImageEnhance.Color(Image.open(img_path).convert("RGB")).enhance(1.18)
    base_scale = min(980 / prod.width, 620 / prod.height)
    bw, bh = max(240, int(prod.width * base_scale)), max(200, int(prod.height * base_scale))
    prod_base = prod.resize((bw, bh), Image.Resampling.LANCZOS)
    accent = (0, 229, 255) if style_idx % 2 == 0 else (250, 204, 21)
    mode = style_idx % 4

    cmd = [
        "ffmpeg", "-y", "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{W}x{H}", "-pix_fmt", "rgb24", "-r", str(fps), "-i", "-",
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", str(out_mp4),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert proc.stdin is not None
    try:
        for f in range(total_frames):
            p = f / max(1, total_frames - 1)
            t = f / fps
            frame = bg.copy()
            d = ImageDraw.Draw(frame)
            ring_r = 280 + int(16 * math.sin(t * 3.5))
            d.ellipse([cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r], outline=accent, width=3)

            dx, dy, scale_f = 0, 0, 1.0
            if p < 0.16:
                ep = 1.0 - (1.0 - p / 0.16) ** 3
                dx = int((1.0 - ep) * (1200 if mode % 2 == 0 else -1200))
                scale_f = 0.85 + 0.15 * ep
            elif p > 0.85:
                sp = ((p - 0.85) / 0.15) ** 2
                dx = int((-1300 if mode % 2 == 0 else 1300) * sp)
            else:
                mid_p = (p - 0.16) / 0.69
                scale_f = 1.0 + 0.10 * mid_p
                dy = int(12 * math.sin(t * 3.0))

            nw, nh = max(140, int(bw * scale_f)), max(120, int(bh * scale_f))
            p_res = prod_base.resize((nw, nh), Image.Resampling.BILINEAR)
            px, py = cx + dx - nw // 2, cy + dy - nh // 2
            d.rounded_rectangle([px - 14, py - 14, px + nw + 14, py + nh + 14], radius=24, fill=(18, 30, 58), outline=accent, width=4)
            frame.paste(p_res, (px, py))
            proc.stdin.write(frame.tobytes())
    finally:
        proc.stdin.close()
        if proc.wait() != 0:
            raise RuntimeError("16:9 dynamic topic animation failed")


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
        q_words = [w.lower() for w in re.findall(r"[a-zA-Z]{3,}", query)]
        for h in hits:
            tags_lower = h.get("tags", "").lower()
            if q_words and tags_lower and not any(w in tags_lower for w in q_words):
                continue
            vids = h.get("videos", {})
            url = (vids.get("large") or vids.get("medium") or {}).get("url")
            if url:
                vid_data = requests.get(url, timeout=60).content
                out_mp4.write_bytes(vid_data)
                return True
    except Exception as exc:
        print(f"[WARN] Pixabay video lookup failed for '{query}': {exc}")

    photo_tmp = out_mp4.with_suffix(".jpg")
    if search_web_topic_photo_16x9(query, photo_tmp) or fetch_topic_image_from_pixabay(query, photo_tmp):
        try:
            render_dynamic_topic_16x9(photo_tmp, out_mp4, duration_sec=6.0, style_idx=abs(hash(query)) % 4)
            return True
        except Exception as exc:
            print(f"[WARN] Dynamic photo fallback failed for '{query}': {exc}")
    return False


def build_long_video(plan: LongVideoPlan) -> tuple[Path, Path, str]:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    font_path = find_malayalam_font()
    segments = []
    chapter_timestamps = []
    elapsed = 0.0

    # Pre-synthesize whoosh SFX for chapter cards & 60s ambient BGM
    whoosh_wav = WORK_DIR / "whoosh.wav"
    bgm_wav = WORK_DIR / "bgm_60s.wav"
    synthesize_whoosh_wav(whoosh_wav)
    synthesize_looping_bgm_60s(bgm_wav)

    # Pre-fetch lead clips from all 5 chapters for a dynamic 5-clip Intro Montage!
    intro_montage_clips: List[Path] = []
    for c_idx, ch in enumerate(plan.chapters, start=1):
        lead_mp4 = WORK_DIR / f"ch_{c_idx}_clip_0.mp4"
        for q in ch.search_queries:
            if fetch_landscape_clip(q, lead_mp4):
                intro_montage_clips.append(lead_mp4)
                break
    if not intro_montage_clips:
        raise RuntimeError("Could not download stock clips from Pixabay.")

    # Intro segment (cycles through all 5 chapters' lead clips!)
    intro_mp3 = WORK_DIR / "intro.mp3"
    intro_dur = tts_to_mp3(plan.intro_narration_ml, intro_mp3)
    per_intro_dur = max(2.5, intro_dur / len(intro_montage_clips))
    intro_trims = []
    for i_idx, m_clip in enumerate(intro_montage_clips):
        it_path = WORK_DIR / f"intro_trim_{i_idx}.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-stream_loop", "-1", "-i", str(m_clip),
                "-t", f"{per_intro_dur:.2f}",
                "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps=30",
                "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "23", str(it_path),
            ],
            check=True,
        )
        intro_trims.append(it_path)
    intro_concat_txt = WORK_DIR / "intro_concat.txt"
    intro_concat_txt.write_text("\n".join(f"file '{p.name}'" for p in intro_trims), encoding="utf-8")
    intro_raw = WORK_DIR / "intro_raw.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(intro_concat_txt), "-c", "copy", str(intro_raw)], check=True)
    intro_seg = WORK_DIR / "seg_intro.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(intro_raw), "-i", str(intro_mp3),
            "-map", "0:v:0", "-map", "1:a:0", "-t", f"{intro_dur:.2f}",
            "-c:v", "copy", "-c:a", "aac", "-ac", "2", "-ar", "44100", "-b:a", "192k", "-shortest", str(intro_seg),
        ],
        check=True,
    )
    segments.append(intro_seg)
    elapsed += intro_dur

    # 5 Chapters
    first_clip_for_thumb = intro_montage_clips[0]
    for idx, ch in enumerate(plan.chapters, start=1):
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        chapter_timestamps.append(f"{mins:02d}:{secs:02d} {ch.chapter_title_ml}")

        # 2.5s Chapter title card + whoosh SFX
        card_png = WORK_DIR / f"card_{idx}.png"
        card_mp4 = WORK_DIR / f"seg_card_{idx}.mp4"
        render_chapter_card(idx, ch.chapter_title_ml, card_png, font_path)
        subprocess.run(
            [
                "ffmpeg", "-y", "-loop", "1", "-i", str(card_png), "-i", str(whoosh_wav),
                "-filter_complex", "[1:a]apad=whole_dur=2.5,volume=0.35[aout]",
                "-map", "0:v:0", "-map", "[aout]",
                "-t", "2.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
                "-c:a", "aac", "-ac", "2", "-ar", "44100", "-b:a", "192k", "-shortest", str(card_mp4),
            ],
            check=True,
        )
        segments.append(card_mp4)
        elapsed += 2.5

        # Chapter narration + 2 landscape clips + 1 custom 2D vector animation clip + callout overlay
        ch_mp3 = WORK_DIR / f"ch_{idx}.mp3"
        ch_srt = WORK_DIR / f"ch_{idx}.srt"
        ch_ass = WORK_DIR / f"ch_{idx}.ass"
        ch_dur = tts_to_mp3(ch.narration_ml, ch_mp3, ch_srt)
        srt_to_karaoke_ass(ch_srt, ch_ass)
        ch_clips = []
        lead_clip = WORK_DIR / f"ch_{idx}_clip_0.mp4"
        if lead_clip.exists():
            ch_clips.append(lead_clip)
        for q_i, q in enumerate(ch.search_queries[1:2], start=1):
            c_path = WORK_DIR / f"ch_{idx}_clip_{q_i}.mp4"
            if fetch_landscape_clip(q, c_path):
                ch_clips.append(c_path)
        if not ch_clips:
            ch_clips = [first_clip_for_thumb]

        # Render 2D vector animation clip in center/upper stage (never covered by lower-third box!)
        anim_mp4 = WORK_DIR / f"ch_{idx}_anim.mp4"
        anim_dur = min(6.0, max(4.0, ch_dur * 0.25))
        render_2d_animation_16x9(ch.animation_scene, ch.chapter_title_ml, anim_dur, anim_mp4, font_path)

        stock_total = max(4.0, ch_dur - anim_dur)
        per_clip_dur = stock_total / len(ch_clips)
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
            if c_i == 0:
                trimmed_clips.append(anim_mp4)

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
                "-filter_complex", f"[0:v][2:v]overlay=0:0,ass={ch_ass.as_posix()}[vout]",
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
            "ffmpeg", "-y", "-stream_loop", "-1", "-i", str(first_clip_for_thumb), "-i", str(outro_mp3),
            "-t", f"{outro_dur:.2f}", "-map", "0:v:0", "-map", "1:a:0", "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps=30",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-ac", "2", "-ar", "44100", "-b:a", "192k", "-shortest", str(outro_seg),
        ],
        check=True,
    )
    segments.append(outro_seg)

    # Concatenate all segments + mix soft looping ambient BGM under voiceover
    full_concat = WORK_DIR / "full_concat.txt"
    full_concat.write_text("\n".join(f"file '{p.name}'" for p in segments), encoding="utf-8")
    merged_vo_mp4 = WORK_DIR / "long_merged_vo.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(full_concat),
            "-c", "copy", str(merged_vo_mp4),
        ],
        check=True,
    )
    final_mp4 = WORK_DIR / "long_video.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(merged_vo_mp4), "-i", str(bgm_wav),
            "-filter_complex",
            "[0:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,volume=1.0[vo];"
            "[1:a]aloop=loop=-1:size=2646000,aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,volume=0.08[bgm];"
            "[vo][bgm]amix=inputs=2:duration=first:normalize=0[aout]",
            "-map", "0:v:0", "-map", "[aout]",
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
    ist_now = datetime.now(IST_TZ)
    target_ist = ist_now.replace(hour=20, minute=30, second=0, microsecond=0)
    if ist_now <= target_ist - timedelta(minutes=12):
        finish_data["published"] = "false"
        finish_data["scheduled_publish_time"] = str(int(target_ist.timestamp()))
        print(f"[INFO] Scheduling Facebook Long Video for 8:30 PM IST ({target_ist.isoformat()})!")
    else:
        finish_data["published"] = "true"
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