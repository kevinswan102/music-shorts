  Automated YouTube Shorts pipeline for music promotion. Generates visualizer-style short-form videos
   from audio tracks and uploads them on a daily schedule.
                                                                                                     
  What it does                                                                                       
                                                                                                     
  1. Sources audio from a configured channel/library                                                 
  2. Analyzes beats — BPM detection, beat mapping for visual sync                                  
  3. Fetches footage — pulls cinematic clips from Pexels API, matched to track mood via LLM          
  4. Renders video — beat-synced cuts, waveform overlays, artist branding                            
  5. Uploads to YouTube — scheduled publishing, auto-comments with streaming/beat store links        
  6. Livestream mode — stitches tracks into a looping livestream video, streams via ffmpeg to YouTube
   RTMP                                                                                              
                                                                                                     
  Architecture                                                                                       
                                                                                                   
  generate_short.py          → orchestrator (daily cron via GitHub Actions)
  ├── music_source.py        → audio sourcing + archive tracking                                     
  ├── beat_analyzer.py       → BPM / beat detection (librosa)                                        
  ├── footage_fetcher.py     → Pexels API footage selection                                          
  ├── llm_client.py          → Groq (Llama 3.3) for descriptions + mood matching                     
  ├── video_renderer.py      → moviepy composition, beat-synced editing                              
  └── youtube_uploader.py    → OAuth upload, scheduled publishing, pinned comments                   
                                                                                                     
  generate_livestream_video.py → builds long-form looping video for 24/7 streams                     
                                                                                                     
  Deployment                                                                                         
                                                                                                   
  Runs entirely on GitHub Actions — no server required.                                              
   
  ┌──────────────────────┬────────────────────┬────────────────────────────────┐                     
  │       Workflow       │      Schedule      │            Purpose             │                   
  ├──────────────────────┼────────────────────┼────────────────────────────────┤                     
  │ generate-short.yml   │ Daily midnight UTC │ Generate + upload 4 Shorts     │
  ├──────────────────────┼────────────────────┼────────────────────────────────┤                     
  │ build-livestream.yml │ Manual             │ Build looping livestream video │                     
  ├──────────────────────┼────────────────────┼────────────────────────────────┤
  │ stream-live.yml      │ Every 6 hours      │ Stream to YouTube RTMP         │                     
  └──────────────────────┴────────────────────┴────────────────────────────────┘                     
   
  Setup                                                                                              
                                                                                                   
  pip install -r requirements.txt

  Required secrets (GitHub Actions → Settings → Secrets):                                            
  - YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET / YOUTUBE_REFRESH_TOKEN — OAuth credentials
  - PEXELS_API_KEY — stock footage                                                                   
  - GROQ_API_KEY — LLM for descriptions                                                            
  - SOURCE_CHANNEL_URL — audio source                                                                
  - YOUTUBE_STREAM_KEY — for livestream mode                                                       
                                                                                                     
  Optional secrets for video metadata:                                                             
  - ARTIST_NAME, BEATSTARS_URL, SPOTIFY_URL, APPLE_MUSIC_URL, HYPERFOLLOW_URL, INSTAGRAM_HANDLE      
                                                                                                     
  YouTube OAuth                                                                                      
                                                                                                     
  python3 youtube_auth_now.py                                                                      
                                                                                                     
  Starts a local server, opens browser for Google OAuth consent, and prints the refresh token to     
  paste into GitHub Actions secrets.                                                                 
                                                                                                     
  Tech Stack                                                                                       

  - Python 3.11 — core pipeline
  - moviepy + ffmpeg — video rendering
  - librosa — audio/beat analysis                                                                    
  - Groq API (Llama 3.3 70B) — LLM for descriptions and mood-based footage selection
  - YouTube Data API v3 — upload, scheduling, comments, livestream management                        
  - Pexels API — royalty-free footage                                                                
  - GitHub Actions — CI/CD and cron scheduling     
