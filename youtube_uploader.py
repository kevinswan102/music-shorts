"""
YouTube Upload Automation — Music Shorts
"""

import os
import json
import logging
import pickle
from typing import Dict, List, Any, Optional
from datetime import datetime

try:
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    YOUTUBE_API_AVAILABLE = True
except ImportError:
    YOUTUBE_API_AVAILABLE = False
    logging.warning("YouTube API libraries not available.")

logger = logging.getLogger(__name__)


class YouTubeUploader:
    """YouTube upload system for music Shorts with OAuth authentication"""

    def __init__(self):
        self.enabled = os.getenv('YOUTUBE_ENABLED', 'false').lower() == 'true'
        self.skip_upload = os.getenv('SKIP_YOUTUBE_UPLOAD', 'true').lower() == 'true'
        self.uploads_today = 0

        self.client_id = os.getenv('YOUTUBE_CLIENT_ID')
        self.client_secret = os.getenv('YOUTUBE_CLIENT_SECRET')
        self.redirect_uri = os.getenv('YOUTUBE_REDIRECT_URI', 'http://localhost:8080/callback')

        self.channel_name = os.getenv('YOUTUBE_CHANNEL_NAME', 'MusicShorts')
        self.privacy_status = os.getenv('YOUTUBE_PRIVACY', 'public')
        self.category_id = os.getenv('YOUTUBE_CATEGORY', '10')  # 10 = Music
        self.default_tags = os.getenv('YOUTUBE_TAGS', 'music,electronic,shorts').split(',')

        self.scopes = ['https://www.googleapis.com/auth/youtube.upload']
        self.credentials_file = 'youtube_credentials.json'
        self.token_file = 'youtube_token.pickle'
        self.youtube_service = None

        logger.info(f"YouTube Uploader initialized - Enabled: {self.enabled}, Skip: {self.skip_upload}")

        if self.enabled and not self.skip_upload:
            self._initialize_youtube_service()

    def _initialize_youtube_service(self) -> bool:
        try:
            if not YOUTUBE_API_AVAILABLE:
                logger.error("YouTube API libraries not available")
                return False
            if not all([self.client_id, self.client_secret]):
                logger.error("YouTube OAuth credentials not configured")
                return False
            credentials = self._get_credentials()
            if not credentials:
                logger.error("Failed to obtain YouTube credentials")
                return False
            self.youtube_service = build('youtube', 'v3', credentials=credentials)
            logger.info("YouTube API service initialized successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize YouTube service: {e}")
            return False

    def _get_credentials(self) -> Optional[Credentials]:
        try:
            # Priority 0: YOUTUBE_REFRESH_TOKEN env var (GitHub Actions / CI path)
            refresh_token_env = os.getenv('YOUTUBE_REFRESH_TOKEN')
            if refresh_token_env:
                creds = Credentials(
                    token=None,
                    refresh_token=refresh_token_env,
                    token_uri='https://oauth2.googleapis.com/token',
                    client_id=self.client_id,
                    client_secret=self.client_secret,
                    scopes=self.scopes,
                )
                try:
                    creds.refresh(Request())
                    logger.info("Credentials built from YOUTUBE_REFRESH_TOKEN env var")
                    return creds
                except Exception as e:
                    logger.warning(f"Env-var refresh token failed: {e}")

            # Priority 1: JSON credentials file
            if os.path.exists(self.credentials_file):
                try:
                    with open(self.credentials_file, 'r') as f:
                        creds_dict = json.load(f)
                    creds = Credentials(
                        token=creds_dict.get('token'),
                        refresh_token=creds_dict.get('refresh_token'),
                        token_uri=creds_dict.get('token_uri'),
                        client_id=creds_dict.get('client_id'),
                        client_secret=creds_dict.get('client_secret'),
                        scopes=creds_dict.get('scopes'),
                    )
                    if creds and creds.valid:
                        return creds
                    elif creds and creds.expired and creds.refresh_token:
                        creds.refresh(Request())
                        updated = {
                            'token': creds.token,
                            'refresh_token': creds.refresh_token,
                            'token_uri': creds.token_uri,
                            'client_id': creds.client_id,
                            'client_secret': creds.client_secret,
                            'scopes': creds.scopes,
                        }
                        with open(self.credentials_file, 'w') as f:
                            json.dump(updated, f, indent=2)
                        return creds
                except Exception as e:
                    logger.warning(f"Could not load credentials from JSON: {e}")

            # Priority 2: pickle fallback
            if os.path.exists(self.token_file):
                try:
                    with open(self.token_file, 'rb') as token:
                        creds = pickle.load(token)
                    if creds and creds.valid:
                        return creds
                    elif creds and creds.expired and creds.refresh_token:
                        creds.refresh(Request())
                        return creds
                except (pickle.PickleError, EOFError):
                    logger.warning("Could not load credentials from pickle")

            logger.error("No valid YouTube credentials found. Run: python3 youtube_auth_now.py")
            return None
        except Exception as e:
            logger.error(f"Error getting credentials: {e}")
            return None

    def upload_video(self, video_data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            if not self.enabled:
                return {'success': False, 'reason': 'YouTube upload disabled', 'mock_upload': True}

            if self.skip_upload:
                metadata = self._generate_metadata(video_data)
                return {
                    'success': False,
                    'mock_upload': True,
                    'reason': 'YouTube upload skipped (dev mode)',
                    'would_upload': {
                        'title': metadata['title'],
                        'description_length': len(metadata['description']),
                        'tags_count': len(metadata['tags']),
                        'privacy': self.privacy_status,
                    },
                }

            if not self.youtube_service:
                if not self._initialize_youtube_service():
                    return {'success': False, 'error': 'YouTube service not available', 'needs_auth': True}

            video_path = video_data.get('video_path')
            if not video_path or not os.path.exists(video_path):
                return {'success': False, 'error': 'Video file not found'}

            metadata = self._generate_metadata(video_data)
            thumbnail_path = video_data.get('thumbnail')
            return self._upload_to_youtube(video_path, metadata, thumbnail_path)

        except Exception as e:
            logger.error(f"Video upload failed: {e}")
            return {'success': False, 'error': str(e)}

    def _upload_to_youtube(self, video_path: str, metadata: Dict, thumbnail_path: Optional[str] = None) -> Dict:
        try:
            logger.info(f"Starting upload: {video_path} ({os.path.getsize(video_path) / (1024*1024):.1f} MB)")

            body = {
                'snippet': {
                    'title': metadata['title'],
                    'description': metadata['description'],
                    'tags': metadata['tags'],
                    'categoryId': self.category_id,
                },
                'status': {
                    'privacyStatus': self.privacy_status,
                    'selfDeclaredMadeForKids': False,
                },
            }

            media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype='video/mp4')
            insert_request = self.youtube_service.videos().insert(
                part=','.join(body.keys()), body=body, media_body=media
            )

            response = None
            retry = 0
            max_retries = 3

            while response is None and retry <= max_retries:
                try:
                    status, response = insert_request.next_chunk()
                    if status:
                        logger.info(f"Upload progress: {int(status.progress() * 100)}%")
                except Exception as upload_error:
                    retry += 1
                    logger.error(f"Upload attempt {retry} failed: {upload_error}")
                    if retry > max_retries:
                        return {'success': False, 'error': f'Upload failed after {max_retries} retries: {upload_error}'}
                    import time
                    time.sleep(2 ** retry)

            if response:
                video_id = response['id']
                video_url = f"https://www.youtube.com/watch?v={video_id}"
                logger.info(f"Upload successful: {video_url}")

                if thumbnail_path and os.path.exists(thumbnail_path):
                    try:
                        self.youtube_service.thumbnails().set(
                            videoId=video_id, media_body=MediaFileUpload(thumbnail_path)
                        ).execute()
                    except Exception as e:
                        logger.warning(f"Thumbnail upload failed: {e}")

                self.uploads_today += 1
                self._post_early_comment(video_id)

                return {'success': True, 'video_id': video_id, 'video_url': video_url, 'title': metadata['title']}

            return {'success': False, 'error': 'Upload completed but no response received'}

        except Exception as e:
            logger.error(f"Upload failed: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return {'success': False, 'error': str(e)}

    def _post_early_comment(self, video_id: str) -> None:
        """Post a comment on the uploaded video — non-fatal."""
        source_channel = os.getenv('SOURCE_CHANNEL_URL', '')
        channel_handle = source_channel.split('/')[-1] if source_channel else ''
        text = (
            f"Full track on {channel_handle} — subscribe for more music!\n\n"
            "Like if you vibed with this one."
        )
        try:
            self.youtube_service.commentThreads().insert(
                part="snippet",
                body={
                    "snippet": {
                        "videoId": video_id,
                        "topLevelComment": {"snippet": {"textOriginal": text}},
                    }
                },
            ).execute()
            logger.info("Comment posted successfully")
        except Exception as e:
            logger.warning(f"Comment failed (non-fatal): {e}")

    def _generate_metadata(self, video_data: Dict[str, Any]) -> Dict[str, Any]:
        """Generate music-focused YouTube metadata."""
        track_name = video_data.get('track_name', 'Untitled')
        artist = video_data.get('artist', os.getenv('ARTIST_NAME', 'Unknown'))
        genre = video_data.get('genre', 'Electronic')
        llm_description = video_data.get('description_text', '')
        source_channel = os.getenv('SOURCE_CHANNEL_URL', '')

        title = f"{track_name} | {artist} #shorts #music #{genre.lower().replace(' ', '')}"
        title = title[:100]

        description = f"{track_name} by {artist}\n\n"
        if llm_description:
            description += f"{llm_description}\n\n"
        if source_channel:
            description += f"Full track: {source_channel}\n\n"
        description += f"#music #{genre.lower()} #shorts #electronic #musicvideo"

        tags = [
            track_name, artist, genre.lower(), 'music', 'shorts',
            'electronic', 'music video', 'visualizer',
        ]

        return {
            'title': title[:100],
            'description': description[:5000],
            'tags': tags[:15],
        }

    def get_upload_stats(self) -> Dict[str, Any]:
        return {
            'enabled': self.enabled,
            'skip_upload': self.skip_upload,
            'uploads_today': self.uploads_today,
            'channel_name': self.channel_name,
            'api_available': YOUTUBE_API_AVAILABLE,
            'service_initialized': self.youtube_service is not None,
        }
