"""
Strava API client using OAuth2.

Authentication flow (handled by the web app):
  1. Redirect user to strava_auth_url(client_id, redirect_uri)
  2. Strava calls back with ?code=...
  3. Call exchange_code(client_id, client_secret, code) → token dict
  4. Store access_token, refresh_token, token_expires_at in settings DB

Subsequent calls use the stored access_token; the client refreshes it
automatically when it is about to expire.
"""

import time

import requests

_API  = "https://www.strava.com/api/v3"
_AUTH = "https://www.strava.com/oauth/authorize"
_TOKEN = "https://www.strava.com/oauth/token"


def strava_auth_url(client_id: str, redirect_uri: str) -> str:
    return (
        f"{_AUTH}?client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&scope=activity:read_all"
    )


def exchange_code(client_id: str, client_secret: str, code: str) -> dict:
    """Exchange an authorization code for tokens. Returns the full token dict."""
    resp = requests.post(_TOKEN, data={
        "client_id":     client_id,
        "client_secret": client_secret,
        "code":          code,
        "grant_type":    "authorization_code",
    })
    resp.raise_for_status()
    return resp.json()


def refresh_token(client_id: str, client_secret: str, refresh_tok: str) -> dict:
    """Refresh an expired access token. Returns the new token dict."""
    resp = requests.post(_TOKEN, data={
        "client_id":     client_id,
        "client_secret": client_secret,
        "grant_type":    "refresh_token",
        "refresh_token": refresh_tok,
    })
    resp.raise_for_status()
    return resp.json()


class StravaClient:
    def __init__(
        self,
        access_token: str,
        client_id: str = "",
        client_secret: str = "",
        refresh_tok: str = "",
        expires_at: float = 0,
        on_refresh=None,
    ):
        """
        access_token   — current bearer token
        client_id/secret — required for automatic token refresh
        refresh_tok    — refresh token (stored in settings DB)
        expires_at     — unix timestamp when access_token expires
        on_refresh     — callback(access_token, refresh_tok, expires_at)
                         called after a successful refresh so the caller
                         can persist the new tokens
        """
        self._client_id     = client_id
        self._client_secret = client_secret
        self._refresh_tok   = refresh_tok
        self._expires_at    = float(expires_at or 0)
        self._on_refresh    = on_refresh

        self._s = requests.Session()
        self._s.headers["Authorization"] = f"Bearer {access_token}"

    def _ensure_fresh(self):
        if not self._client_id or not self._refresh_tok:
            return
        if self._expires_at and time.time() < self._expires_at - 60:
            return
        data = refresh_token(self._client_id, self._client_secret, self._refresh_tok)
        self._s.headers["Authorization"] = f"Bearer {data['access_token']}"
        self._refresh_tok = data["refresh_token"]
        self._expires_at  = data["expires_at"]
        if self._on_refresh:
            self._on_refresh(data["access_token"], data["refresh_token"], data["expires_at"])

    # ── Public API ──────────────────────────────────────────────────────────

    def get_activity_ids(self, n: int = 10) -> list[int]:
        self._ensure_fresh()
        resp = self._s.get(f"{_API}/athlete/activities", params={"per_page": n})
        resp.raise_for_status()
        return [a["id"] for a in resp.json()]

    def get_activity(self, activity_id: int) -> dict:
        self._ensure_fresh()
        detail  = self._get_detail(activity_id)
        streams = self._get_streams(activity_id)
        return _build_activity(detail, streams)

    # ── Private helpers ─────────────────────────────────────────────────────

    def _get_detail(self, activity_id: int) -> dict:
        resp = self._s.get(f"{_API}/activities/{activity_id}")
        resp.raise_for_status()
        return resp.json()

    def _get_streams(self, activity_id: int) -> dict:
        keys = "latlng,altitude,heartrate,watts,time"
        resp = self._s.get(
            f"{_API}/activities/{activity_id}/streams",
            params={"keys": keys, "key_by_type": "true"},
        )
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        return resp.json()


def _build_activity(detail: dict, streams: dict) -> dict:
    """Map Strava API response to Bikeodon's internal activity dict."""
    latlng   = streams.get("latlng",    {}).get("data", [])
    altitude = streams.get("altitude",  {}).get("data", [])
    hr       = streams.get("heartrate", {}).get("data", [])
    watts    = streams.get("watts",     {}).get("data", [])
    time_s   = streams.get("time",      {}).get("data", [])

    n = len(latlng)

    def _at(lst, i):
        return lst[i] if i < len(lst) else None

    points = []
    for i in range(n):
        pair = latlng[i]
        points.append([
            pair[0], pair[1],
            _at(altitude, i),
            _at(hr, i),
            _at(watts, i),
            _at(time_s, i),
        ])

    start_latlng = detail.get("start_latlng") or [None, None]

    return {
        "id":                   detail["id"],
        "name":                 detail.get("name"),
        "sport_type":           detail.get("sport_type") or detail.get("type", "Ride"),
        "start_date":           detail.get("start_date"),
        "distance":             detail.get("distance"),
        "moving_time":          detail.get("moving_time"),
        "elapsed_time":         detail.get("elapsed_time"),
        "total_elevation_gain": detail.get("total_elevation_gain"),
        "max_speed":            detail.get("max_speed"),
        "average_heartrate":    detail.get("average_heartrate"),
        "max_heartrate":        detail.get("max_heartrate"),
        "average_watts":        detail.get("average_watts"),
        "max_watts":            detail.get("max_watts"),
        "start_lat":            start_latlng[0],
        "start_lon":            start_latlng[1],
        "points":               points,
    }
