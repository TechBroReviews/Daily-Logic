# Daily Logic Malayalam — Automated Daily Shorts & Reels

This repository automatically discovers an everyday science, how-things-work, or fact-check topic every day, writes a 50-second Malayalam script using the Gemini API, synthesizes a Malayalam voiceover (`ml-IN-MidhunNeural`) and `.srt` subtitles via `edge-tts`, stitches vertical 1080x1920 video clips via FFmpeg, and uploads to **YouTube Shorts** (and optionally **Instagram Reels** & **Facebook Reels**) at 6:00 PM IST.

## Quick 3-Minute Setup

1. **Upload Files**: Upload all files from this ZIP into the root of your private GitHub repository (making sure `.github/workflows/daily_youtube_shorts.yml` stays in its `.github/workflows/` folder).
2. **Add GitHub Repository Secrets** (**Settings -> Secrets and variables -> Actions -> New repository secret**):
   - `GEMINI_API_KEY`: Your API key from [Google AI Studio](https://aistudio.google.com/app/apikey)
   - `PIXABAY_API_KEY`: Your free API key from [Pixabay API Docs](https://pixabay.com/api/docs/) (optional — falls back to clean animated backgrounds if omitted)
   - `YT_CLIENT_ID`: Your Google Cloud OAuth 2.0 Client ID
   - `YT_CLIENT_SECRET`: Your Google Cloud OAuth 2.0 Client Secret
   - `YT_REFRESH_TOKEN`: Your YouTube Refresh Token from [Google OAuth Playground](https://developers.google.com/oauthplayground/) (scope: `https://www.googleapis.com/auth/youtube.upload`)
3. **Run Your First Video**: Open the **Actions** tab in your GitHub repository, click **Daily Logic Malayalam Shorts + Reels Automation**, and click **Run workflow**.
