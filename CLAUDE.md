# CLAUDE.md — music-shorts

## This project
Automated YouTube Shorts pipeline for @StarDriftMusic channel
- Entry point: `generate_short.py`
- Pipeline: music_source.py → beat_analyzer.py → footage_fetcher.py → video_renderer.py → youtube_uploader.py
- Runs on GitHub Actions (free tier), cron daily at midnight UTC (6pm EST)
- Source music: @official_stardrift YouTube channel
- Stock footage: Pexels API (free, CC0)

## LLM switching (no code change needed)
Set `LLM_PROVIDER` env var:
- `groq`   = free (Llama 3.3 70B) — needs GROQ_API_KEY from console.groq.com
- `openai` = paid (GPT-4o-mini) — needs OPENAI_API_KEY

## GitHub Actions
- Workflow: `.github/workflows/generate-short.yml`
- Cron: midnight UTC daily (6pm EST)
- Secrets: YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN, PEXELS_API_KEY, GROQ_API_KEY
- archive.txt committed back to repo after each run

## YouTube OAuth
Separate channel with its own YOUTUBE_REFRESH_TOKEN
Run `python3 youtube_auth_now.py` with @StarDriftMusic active in browser

## moviepy==1.0.3 pinned (v2.x broke imports)
## Video rendering: ffmpeg filter chain for color grading, stream-copy concat
