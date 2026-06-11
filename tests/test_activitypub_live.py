"""
Live smoke tests for ActivityPub endpoints on bikeodon.org.

These hit the real HTTPS server — run them after deploying to verify the
federation layer is reachable and spec-compliant from the outside.

Run with:
    pytest tests/test_activitypub_live.py -v
"""

import pytest
import requests

BASE    = "https://bikeodon.org"
USERNAME = "tim0s42"
ACCT    = f"acct:{USERNAME}@bikeodon.org"


@pytest.fixture(scope="module")
def webfinger():
    r = requests.get(
        f"{BASE}/.well-known/webfinger",
        params={"resource": ACCT},
        timeout=10,
    )
    return r


@pytest.fixture(scope="module")
def actor(webfinger):
    actor_url = next(
        l["href"] for l in webfinger.json()["links"] if l["rel"] == "self"
    )
    r = requests.get(
        actor_url,
        headers={"Accept": "application/activity+json"},
        timeout=10,
    )
    return r


# ---------------------------------------------------------------------------
# WebFinger
# ---------------------------------------------------------------------------

class TestWebFingerLive:

    def test_status_200(self, webfinger):
        assert webfinger.status_code == 200

    def test_content_type_is_jrd(self, webfinger):
        assert "application/jrd+json" in webfinger.headers["Content-Type"]

    def test_subject(self, webfinger):
        assert webfinger.json()["subject"] == ACCT

    def test_self_link_present(self, webfinger):
        links = webfinger.json().get("links", [])
        self_links = [l for l in links if l.get("rel") == "self"]
        assert len(self_links) == 1

    def test_self_link_is_https(self, webfinger):
        self_link = next(l for l in webfinger.json()["links"] if l["rel"] == "self")
        assert self_link["href"].startswith("https://bikeodon.org")

    def test_unknown_account_404(self):
        r = requests.get(
            f"{BASE}/.well-known/webfinger",
            params={"resource": "acct:nobody@bikeodon.org"},
            timeout=10,
        )
        assert r.status_code == 404

    def test_cross_domain_404(self):
        r = requests.get(
            f"{BASE}/.well-known/webfinger",
            params={"resource": f"acct:{USERNAME}@evil.example"},
            timeout=10,
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Actor
# ---------------------------------------------------------------------------

class TestActorLive:

    def test_status_200(self, actor):
        assert actor.status_code == 200

    def test_content_type(self, actor):
        assert "application/activity+json" in actor.headers["Content-Type"]

    def test_type_is_person(self, actor):
        assert actor.json()["type"] == "Person"

    def test_id_is_canonical(self, actor):
        assert actor.json()["id"] == f"{BASE}/users/{USERNAME}"

    def test_preferred_username(self, actor):
        assert actor.json()["preferredUsername"] == USERNAME

    def test_has_inbox(self, actor):
        assert actor.json()["inbox"].startswith("https://bikeodon.org")

    def test_has_outbox(self, actor):
        assert actor.json()["outbox"].startswith("https://bikeodon.org")

    def test_has_public_key(self, actor):
        pk = actor.json().get("publicKey", {})
        assert pk["publicKeyPem"].startswith("-----BEGIN PUBLIC KEY-----")
        assert pk["owner"] == f"{BASE}/users/{USERNAME}"

    def test_has_display_name(self, actor):
        assert "name" in actor.json()
        assert len(actor.json()["name"]) > 0

    def test_has_summary(self, actor):
        assert "summary" in actor.json()
        assert len(actor.json()["summary"]) > 0

    def test_has_icon(self, actor):
        icon = actor.json().get("icon", {})
        assert icon.get("type") == "Image"
        assert "mediaType" in icon
        assert icon.get("url", "").startswith("https://bikeodon.org")

    def test_avatar_is_reachable(self, actor):
        url = actor.json()["icon"]["url"]
        r = requests.get(url, timeout=10)
        assert r.status_code == 200
        assert r.headers["Content-Type"].startswith("image/")


# ---------------------------------------------------------------------------
# Followers collection
# ---------------------------------------------------------------------------

class TestFollowersLive:

    def test_followers_status_200(self):
        r = requests.get(
            f"{BASE}/users/{USERNAME}/followers",
            headers={"Accept": "application/activity+json"},
            timeout=10,
        )
        assert r.status_code == 200

    def test_followers_content_type(self):
        r = requests.get(
            f"{BASE}/users/{USERNAME}/followers",
            headers={"Accept": "application/activity+json"},
            timeout=10,
        )
        assert "application/activity+json" in r.headers["Content-Type"]

    def test_followers_is_ordered_collection(self):
        r = requests.get(
            f"{BASE}/users/{USERNAME}/followers",
            headers={"Accept": "application/activity+json"},
            timeout=10,
        )
        data = r.json()
        assert data["type"] == "OrderedCollection"

    def test_followers_has_total_items(self):
        r = requests.get(
            f"{BASE}/users/{USERNAME}/followers",
            headers={"Accept": "application/activity+json"},
            timeout=10,
        )
        data = r.json()
        assert "totalItems" in data
        assert isinstance(data["totalItems"], int)

    def test_followers_has_ordered_items(self):
        r = requests.get(
            f"{BASE}/users/{USERNAME}/followers",
            headers={"Accept": "application/activity+json"},
            timeout=10,
        )
        data = r.json()
        assert "orderedItems" in data
        assert isinstance(data["orderedItems"], list)

    def test_followers_unknown_user_404(self):
        r = requests.get(
            f"{BASE}/users/nobody/followers",
            headers={"Accept": "application/activity+json"},
            timeout=10,
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------

class TestInboxLive:

    def test_inbox_rejects_get(self):
        r = requests.get(
            f"{BASE}/users/{USERNAME}/inbox",
            headers={"Accept": "application/activity+json"},
            timeout=10,
        )
        assert r.status_code == 405

    def test_inbox_rejects_unsigned_request(self):
        r = requests.post(
            f"{BASE}/users/{USERNAME}/inbox",
            data="not json",
            headers={"Content-Type": "text/plain"},
            timeout=10,
        )
        assert r.status_code == 401

    def test_inbox_unknown_user_404(self):
        r = requests.post(
            f"{BASE}/users/nobody/inbox",
            json={"type": "Follow", "actor": "https://example.com/users/test"},
            timeout=10,
        )
        assert r.status_code == 404
