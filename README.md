# music-shorts

End-to-end content automation pipeline that generates, renders, and publishes short-form music videos on a daily schedule — fully autonomous, zero manual intervention.

## System Design

- **Audio analysis** — BPM detection and beat mapping (librosa) to drive visual sync
- **Content sourcing** — programmatic footage selection via Pexels API, mood-matched using LLM inference (Groq/Llama 3.3)
- **Video rendering** — beat-synced clip editing, text overlays, and branding composited with moviepy + ffmpeg
- **Publishing** — YouTube Data API v3 OAuth flow: batched scheduled uploads, metadata generation, automated engagement
- **Livestream** — separate pipeline that builds a looping video and streams 24/7 via ffmpeg RTMP
- **Scheduling** — GitHub Actions cron, no infrastructure cost

## Technical Highlights

- Fully serverless — runs on GitHub Actions free tier
- Handles OAuth token refresh, retry logic, and graceful degradation
- Reddit API integration for dynamic text overlay content (rotates across multiple sources)
- Processes 4 videos/day end-to-end in under 30 minutes
- Archive tracking prevents duplicate content

## Stack

Python 3.11 · moviepy · ffmpeg · librosa · YouTube Data API v3 · Groq API · Pexels API · GitHub Actions
