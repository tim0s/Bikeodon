"""
Mastodon posting client.
Uses the standard Mastodon API — no third-party library needed.
"""

import os
import time

import requests


class MastodonClient:
    def __init__(self, instance: str, access_token: str):
        self._base = instance.rstrip("/")
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {access_token}"

    @classmethod
    def from_env(cls, instance: str = "https://mastodon.social") -> "MastodonClient":
        token = os.environ.get("MASTODON_TOKEN", "").strip()
        if not token:
            raise ValueError(
                "MASTODON_TOKEN environment variable is not set.\n"
                "  1. Go to mastodon.social/settings/applications\n"
                "  2. Create a new application\n"
                "  3. Copy the access token\n"
                "  4. export MASTODON_TOKEN=<value>"
            )
        return cls(instance, token)

    def upload_image(self, image_path: str, description: str = "") -> str:
        """Upload an image and return its media ID."""
        with open(image_path, "rb") as f:
            resp = self._session.post(
                f"{self._base}/api/v2/media",
                files={"file": (os.path.basename(image_path), f, "image/png")},
                data={"description": description},
            )
        resp.raise_for_status()
        media = resp.json()
        media_id = media["id"]

        # v2 media upload is async — poll until processing is done
        for _ in range(10):
            if media.get("url"):
                break
            time.sleep(1)
            r = self._session.get(f"{self._base}/api/v1/media/{media_id}")
            if r.status_code == 200:
                media = r.json()

        return media_id

    def post(self, text: str, image_path: str | None = None,
             alt_text: str = "", visibility: str = "public") -> dict:
        """
        Create a status, optionally with an image attachment.
        Returns the created status dict.
        """
        media_ids = []
        if image_path:
            print(f"  Uploading image…")
            media_ids.append(self.upload_image(image_path, description=alt_text))

        resp = self._session.post(
            f"{self._base}/api/v1/statuses",
            json={
                "status":     text,
                "media_ids":  media_ids,
                "visibility": visibility,
            },
        )
        resp.raise_for_status()
        return resp.json()
