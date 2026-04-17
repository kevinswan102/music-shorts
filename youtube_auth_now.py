#!/usr/bin/env python3
"""
One-command YouTube OAuth fix.
Starts a real local server on :8080, opens the auth URL, catches the callback
automatically, exchanges for tokens, saves token.json and prints the
refresh_token you need to paste into GitHub Actions secrets.

Usage:
    python3 youtube_auth_now.py
"""
import os, json, urllib.parse, webbrowser, sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv

load_dotenv()

REDIRECT_URI = "http://localhost:8080/callback"
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.force-ssl"

_auth_code = None


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        global _auth_code
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code = qs.get("code", [None])[0]
        error = qs.get("error", [None])[0]

        if error:
            body = f"<h2>Auth error: {error}</h2><p>Close this tab and try again.</p>"
            self.send_response(400)
        elif code:
            _auth_code = code
            body = "<h2>Authorization successful!</h2><p>You can close this tab and return to the terminal.</p>"
            self.send_response(200)
        else:
            body = "<h2>No code received.</h2>"
            self.send_response(400)

        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass  # silence server logs


def _exchange(client_id, client_secret, code):
    import urllib.request, urllib.error
    data = urllib.parse.urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def main():
    client_id = os.getenv("YOUTUBE_CLIENT_ID") or os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("YOUTUBE_CLIENT_SECRET") or os.getenv("GOOGLE_CLIENT_SECRET")

    if not client_id or not client_secret:
        print("Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET in .env first.")
        sys.exit(1)

    auth_url = (
        "https://accounts.google.com/o/oauth2/auth"
        f"?client_id={urllib.parse.quote(client_id)}"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
        "&response_type=code"
        f"&scope={urllib.parse.quote(SCOPES)}"
        "&access_type=offline"
        "&prompt=consent"
    )

    print("\nYouTube OAuth — Music channel setup")
    print("=" * 50)
    print("1. A browser tab will open.")
    print("2. Sign in to your Google account.")
    print("3. IMPORTANT: Switch to the correct YouTube channel")
    print("   (click your profile picture -> switch account/channel)")
    print("   BEFORE clicking Allow — this locks the token to that channel.")
    print("4. Click Allow — this terminal catches the response automatically.")
    print("5. Done — refresh token printed here + saved to token.json.\n")
    input("Press ENTER to open the browser...")

    server = HTTPServer(("localhost", 8080), _Handler)
    server.timeout = 1  # non-blocking poll

    webbrowser.open(auth_url)
    print("\nWaiting for browser callback (2-minute timeout)...")

    import time
    deadline = time.time() + 120
    while _auth_code is None and time.time() < deadline:
        server.handle_request()

    server.server_close()

    if _auth_code is None:
        print("Timed out waiting for auth. Run the script again.")
        sys.exit(1)

    print("Got authorization code — exchanging for tokens...")

    try:
        tokens = _exchange(client_id, client_secret, _auth_code)
    except Exception as e:
        print(f"Token exchange failed: {e}")
        sys.exit(1)

    if "refresh_token" not in tokens:
        print(f"No refresh_token in response: {tokens}")
        print("Try revoking access at https://myaccount.google.com/permissions and run again.")
        sys.exit(1)

    # Save locally (raw Google token format)
    with open("token.json", "w") as f:
        json.dump(tokens, f, indent=2)

    # Also save in the format expected by youtube_uploader.py
    uploader_creds = {
        "token":          tokens.get("access_token"),
        "refresh_token":  tokens["refresh_token"],
        "token_uri":      "https://oauth2.googleapis.com/token",
        "client_id":      client_id,
        "client_secret":  client_secret,
        "scopes":         SCOPES.split(),
    }
    with open("youtube_credentials.json", "w") as f:
        json.dump(uploader_creds, f, indent=2)
    print("Saved youtube_credentials.json (uploader format)")

    rt = tokens["refresh_token"]
    print(f"\nOAuth complete!\n")
    print("-" * 50)
    print(f"REFRESH TOKEN:\n{rt}")
    print("-" * 50)
    print("\nNext steps:")
    print("  1. GitHub repo -> Settings -> Secrets and variables -> Actions")
    print(f"     YOUTUBE_REFRESH_TOKEN = {rt[:40]}...")
    print("  2. Also add YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET")
    print("  3. Trigger workflow_dispatch or wait for cron schedule")
    print("\ntoken.json also saved locally for direct runs.")


if __name__ == "__main__":
    main()
