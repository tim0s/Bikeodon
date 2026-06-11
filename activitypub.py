"""
ActivityPub federation layer.

Registered as a Flask Blueprint in app.py.  Implements:

  /.well-known/webfinger   — account discovery (RFC 7033)
  /users/<username>        — Actor document (JSON-LD) or public HTML profile

Subsequent layers (inbox, outbox, delivery) will live here too.
"""

import base64
import hashlib
import json
import logging
import re
import requests
from datetime import datetime, timezone
from email.utils import formatdate, parsedate_to_datetime
from urllib.parse import urlparse

from flask import (
    Blueprint, abort, current_app, jsonify, redirect,
    render_template, request, url_for,
)
from cryptography.hazmat.primitives.asymmetric import rsa, padding as asym_padding
from cryptography.hazmat.primitives import hashes, serialization

from database import (
    get_user_by_username,
    add_follower, remove_follower, get_followers,
    add_following, accept_following,
)
from database import _conn as _db_conn

_log = logging.getLogger(__name__)

bp = Blueprint("activitypub", __name__)

_AP_MIME  = "application/activity+json"
_JRD_MIME = "application/jrd+json"

_AP_CONTEXT = [
    "https://www.w3.org/ns/activitystreams",
    "https://w3id.org/security/v1",
]

# In-memory cache for remote public keys: key_id -> PEM string.
# Cleared on restart; keys rarely change so this is safe.
_pubkey_cache: dict[str, str] = {}


# ---------------------------------------------------------------------------
# HTTP Signature verification (incoming requests)
# ---------------------------------------------------------------------------

def _parse_signature_header(header: str) -> dict:
    """Parse 'keyId="...",algorithm="...",headers="...",signature="..."' into a dict."""
    return {k: v for k, v in re.findall(r'(\w+)="([^"]*)"', header)}


def _fetch_public_key_pem(key_id: str) -> str | None:
    """Fetch the PEM public key for a keyId URL, with in-memory caching."""
    if key_id in _pubkey_cache:
        return _pubkey_cache[key_id]
    actor_url = key_id.split("#")[0]
    try:
        r = requests.get(actor_url, headers={"Accept": _AP_MIME}, timeout=10)
        if not r.ok:
            return None
        pk = r.json().get("publicKey", {})
        if isinstance(pk, dict) and pk.get("id") == key_id:
            pem = pk.get("publicKeyPem")
            if pem:
                _pubkey_cache[key_id] = pem
            return pem
    except Exception as exc:
        _log.warning("Could not fetch public key %s: %s", key_id, exc)
    return None


def _verify_http_signature(req, body_bytes: bytes) -> tuple[bool, str]:
    """
    Verify the HTTP Signature on an incoming request.
    Returns (ok, reason_string).
    """
    from cryptography.exceptions import InvalidSignature as _InvalidSig

    sig_header = req.headers.get("Signature", "")
    if not sig_header:
        return False, "missing Signature header"

    params   = _parse_signature_header(sig_header)
    key_id   = params.get("keyId", "")
    hdr_list = params.get("headers", "date").split()
    sig_b64  = params.get("signature", "")

    if not key_id or not sig_b64:
        return False, "incomplete Signature header"

    # 1. Verify Digest matches body
    if "digest" in hdr_list:
        digest_hdr = req.headers.get("Digest", "")
        if not digest_hdr.startswith("SHA-256="):
            return False, "unsupported or missing Digest"
        expected = base64.b64encode(hashlib.sha256(body_bytes).digest()).decode()
        if digest_hdr[8:] != expected:
            return False, "digest mismatch"

    # 2. Verify Date is fresh (±5 minutes)
    if "date" in hdr_list:
        date_hdr = req.headers.get("Date", "")
        try:
            req_time = parsedate_to_datetime(date_hdr)
            delta = abs((datetime.now(timezone.utc) - req_time).total_seconds())
            if delta > 300:
                return False, f"request date too far from now ({delta:.0f}s)"
        except Exception:
            return False, "invalid or missing Date header"

    # 3. Fetch sender's public key
    pub_pem = _fetch_public_key_pem(key_id)
    if not pub_pem:
        return False, f"could not fetch public key for {key_id}"

    # 4. Reconstruct signing string and verify RSA-SHA256
    def _hdr_val(h):
        if h == "(request-target)":
            return f"post {req.path}"
        return req.headers.get(h, "")

    signing_string = "\n".join(f"{h}: {_hdr_val(h)}" for h in hdr_list)

    try:
        pub_key = serialization.load_pem_public_key(
            pub_pem.encode() if isinstance(pub_pem, str) else pub_pem
        )
        pub_key.verify(
            base64.b64decode(sig_b64),
            signing_string.encode(),
            asym_padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True, "ok"
    except _InvalidSig:
        return False, "signature verification failed"
    except Exception as exc:
        return False, f"verification error: {exc}"


# ---------------------------------------------------------------------------
# RSA keypair — generated once per user, stored in settings table
# ---------------------------------------------------------------------------

def get_or_create_keypair(db_path: str, user_id: int) -> tuple[str, str]:
    """Return (public_pem, private_pem) for a user, generating if absent."""
    conn = _db_conn(db_path)
    row  = conn.execute(
        "SELECT public_key_pem, private_key_pem FROM users WHERE id=?", (user_id,)
    ).fetchone()

    if row and row["public_key_pem"] and row["private_key_pem"]:
        return row["public_key_pem"], row["private_key_pem"]

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    pub_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    conn.execute(
        "UPDATE users SET public_key_pem=?, private_key_pem=? WHERE id=?",
        (pub_pem, priv_pem, user_id),
    )
    conn.commit()
    return pub_pem, priv_pem


# ---------------------------------------------------------------------------
# WebFinger  /.well-known/webfinger
# ---------------------------------------------------------------------------

@bp.route("/.well-known/webfinger")
def webfinger():
    resource = request.args.get("resource", "").strip()
    if not resource:
        abort(400)
    if not resource.startswith("acct:"):
        abort(400)

    acct = resource[len("acct:"):]
    if "@" not in acct:
        abort(400)
    username, domain = acct.rsplit("@", 1)

    # Only serve accounts on our own domain (strip port for comparison)
    our_domain = request.host.split(":")[0]
    if domain != our_domain:
        abort(404)

    db_path = current_app.config["DB_PATH"]
    user = get_user_by_username(db_path, username)
    if not user:
        abort(404)

    actor_url = url_for("activitypub.actor", username=username, _external=True)
    resp = jsonify({
        "subject": f"acct:{username}@{domain}",
        "links": [
            {
                "rel":  "self",
                "type": _AP_MIME,
                "href": actor_url,
            }
        ],
    })
    resp.content_type = _JRD_MIME
    return resp


# ---------------------------------------------------------------------------
# Actor  /users/<username>
# ---------------------------------------------------------------------------

@bp.route("/users/<username>")
def actor(username):
    accept = request.headers.get("Accept", "")
    wants_ap = _AP_MIME in accept or "application/ld+json" in accept

    db_path = current_app.config["DB_PATH"]
    user = get_user_by_username(db_path, username)
    if not user:
        abort(404)

    if not wants_ap:
        # Browser visit — render the public profile page
        return render_template("profile.html", profile_user=dict(user))

    pub_pem, _ = get_or_create_keypair(db_path, user["id"])
    actor_url  = url_for("activitypub.actor",     username=username, _external=True)
    inbox_url  = url_for("activitypub.inbox",      username=username, _external=True)
    outbox_url = url_for("activitypub.outbox",     username=username, _external=True)
    foll_url   = url_for("activitypub.followers",  username=username, _external=True)
    fing_url   = url_for("activitypub.following",  username=username, _external=True)
    avatar_url = url_for("user_avatar", username=username, _external=True)

    doc = {
        "@context":          _AP_CONTEXT,
        "id":                actor_url,
        "type":              "Person",
        "preferredUsername": username,
        "inbox":             inbox_url,
        "outbox":            outbox_url,
        "followers":         foll_url,
        "following":         fing_url,
        "publicKey": {
            "id":           f"{actor_url}#main-key",
            "owner":        actor_url,
            "publicKeyPem": pub_pem,
        },
        "icon": {
            "type":      "Image",
            "mediaType": _avatar_media_type(dict(user).get("avatar_filename")),
            "url":       avatar_url,
        },
    }

    u = dict(user)
    if u.get("display_name"):
        doc["name"] = u["display_name"]
    if u.get("summary"):
        doc["summary"] = u["summary"]

    resp = jsonify(doc)
    resp.content_type = _AP_MIME
    return resp


def _avatar_media_type(filename: str | None) -> str:
    if not filename:
        return "image/png"
    ext = filename.rsplit(".", 1)[-1].lower()
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "gif": "image/gif",
            "webp": "image/webp"}.get(ext, "image/png")


# ---------------------------------------------------------------------------
# Inbox  POST /users/<username>/inbox
# ---------------------------------------------------------------------------

@bp.route("/users/<username>/inbox", methods=["POST"])
def inbox(username):
    db_path = current_app.config["DB_PATH"]
    user = get_user_by_username(db_path, username)
    if not user:
        abort(404)

    raw = request.get_data()

    ok, reason = _verify_http_signature(request, raw)
    if not ok:
        _log.warning("Inbox %s: rejected request — %s", username, reason)
        abort(401)

    try:
        activity = json.loads(raw)
        if not isinstance(activity, dict):
            abort(400)
    except (ValueError, TypeError):
        abort(400)

    activity_type = activity.get("type")

    if activity_type == "Follow":
        _handle_follow(username, user, activity, db_path)
    elif activity_type == "Accept":
        obj = activity.get("object", {})
        if isinstance(obj, dict) and obj.get("type") == "Follow":
            remote_actor = activity.get("actor", "")
            if remote_actor:
                accept_following(db_path, username, remote_actor)
    elif activity_type == "Undo":
        obj = activity.get("object", {})
        if isinstance(obj, dict) and obj.get("type") == "Follow":
            _handle_undo_follow(username, activity, db_path)

    return "", 202


def _handle_follow(local_username, local_user, activity, db_path):
    actor_url = activity.get("actor")
    if not actor_url:
        return

    # Fetch remote actor to discover inbox URL and profile info
    inbox_url = ""
    display_name = None
    avatar_url = None
    try:
        resp = requests.get(
            actor_url,
            headers={"Accept": _AP_MIME},
            timeout=10,
        )
        actor_doc = resp.json()
        inbox_url    = actor_doc.get("inbox", "")
        display_name = actor_doc.get("name") or actor_doc.get("preferredUsername")
        icon = actor_doc.get("icon", {})
        if isinstance(icon, dict):
            avatar_url = icon.get("url")
    except Exception as exc:
        _log.warning("Could not fetch remote actor %s: %s", actor_url, exc)

    add_follower(db_path, local_username, actor_url, inbox_url,
                 display_name=display_name, avatar_url=avatar_url)

    if not inbox_url:
        return

    actor_ap_url = url_for("activitypub.actor", username=local_username, _external=True)
    _, priv_pem = get_or_create_keypair(db_path, local_user["id"])
    key_id = f"{actor_ap_url}#main-key"

    accept = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{actor_ap_url}#accept/{abs(hash(activity.get('id', actor_url))):08x}",
        "type": "Accept",
        "actor": actor_ap_url,
        "object": activity,
    }
    _deliver_activity(inbox_url, accept, priv_pem, key_id)


def _handle_undo_follow(local_username, activity, db_path):
    actor_url = activity.get("actor")
    if actor_url:
        remove_follower(db_path, local_username, actor_url)


def webfinger_lookup(handle: str) -> dict | None:
    """
    Resolve a fediverse handle (e.g. alice@mastodon.social or @alice@mastodon.social)
    to an actor document dict, or return None on any failure.
    """
    handle = handle.strip().lstrip("@")
    if "@" not in handle:
        return None
    username, domain = handle.rsplit("@", 1)
    try:
        wf = requests.get(
            f"https://{domain}/.well-known/webfinger",
            params={"resource": f"acct:{username}@{domain}"},
            headers={"Accept": _JRD_MIME},
            timeout=10,
        )
        if not wf.ok:
            return None
        actor_url = next(
            (l["href"] for l in wf.json().get("links", []) if l.get("rel") == "self"),
            None,
        )
        if not actor_url:
            return None
        ar = requests.get(actor_url, headers={"Accept": _AP_MIME}, timeout=10)
        if not ar.ok:
            return None
        doc = ar.json()
        icon = doc.get("icon", {})
        return {
            "actor_url":    actor_url,
            "display_name": doc.get("name") or doc.get("preferredUsername"),
            "handle":       f"@{doc.get('preferredUsername', username)}@{domain}",
            "avatar_url":   icon.get("url") if isinstance(icon, dict) else None,
        }
    except Exception as exc:
        _log.warning("WebFinger lookup failed for %s: %s", handle, exc)
        return None


def send_follow(local_username, local_user, remote_actor_url, db_path):
    """Send a Follow activity to a remote actor and record it as pending."""
    try:
        resp = requests.get(remote_actor_url, headers={"Accept": _AP_MIME}, timeout=10)
        actor_doc    = resp.json()
        inbox_url    = actor_doc.get("inbox", "")
        display_name = actor_doc.get("name") or actor_doc.get("preferredUsername")
        icon         = actor_doc.get("icon", {})
        avatar_url   = icon.get("url") if isinstance(icon, dict) else None
    except Exception as exc:
        _log.warning("Could not fetch remote actor %s: %s", remote_actor_url, exc)
        return

    actor_ap_url = url_for("activitypub.actor", username=local_username, _external=True)
    _, priv_pem  = get_or_create_keypair(db_path, local_user["id"])
    key_id       = f"{actor_ap_url}#main-key"

    follow_activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{actor_ap_url}#follow/{abs(hash(remote_actor_url)):08x}",
        "type": "Follow",
        "actor": actor_ap_url,
        "object": remote_actor_url,
    }

    add_following(db_path, local_username, remote_actor_url, inbox_url,
                  display_name=display_name, avatar_url=avatar_url)

    if inbox_url:
        _deliver_activity(inbox_url, follow_activity, priv_pem, key_id)


def _deliver_activity(inbox_url, activity_doc, private_pem, key_id):
    body = json.dumps(activity_doc).encode()
    headers = _sign_headers("POST", inbox_url, body, private_pem, key_id)
    try:
        requests.post(inbox_url, data=body, headers=headers, timeout=10)
    except Exception as exc:
        _log.warning("Delivery to %s failed: %s", inbox_url, exc)


def _sign_headers(method, url, body_bytes, private_pem, key_id):
    parsed = urlparse(url)
    host = parsed.netloc
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    date = formatdate(usegmt=True)
    digest = "SHA-256=" + base64.b64encode(hashlib.sha256(body_bytes).digest()).decode()

    signing_string = "\n".join([
        f"(request-target): {method.lower()} {path}",
        f"host: {host}",
        f"date: {date}",
        f"digest: {digest}",
    ])

    private_key = serialization.load_pem_private_key(
        private_pem.encode() if isinstance(private_pem, str) else private_pem,
        password=None,
    )
    sig_b64 = base64.b64encode(
        private_key.sign(signing_string.encode(), asym_padding.PKCS1v15(), hashes.SHA256())
    ).decode()

    return {
        "Host": host,
        "Date": date,
        "Digest": digest,
        "Content-Type": _AP_MIME,
        "Signature": (
            f'keyId="{key_id}",algorithm="rsa-sha256",'
            f'headers="(request-target) host date digest",'
            f'signature="{sig_b64}"'
        ),
    }


# ---------------------------------------------------------------------------
# Followers  GET /users/<username>/followers
# ---------------------------------------------------------------------------

@bp.route("/users/<username>/followers")
def followers(username):
    db_path = current_app.config["DB_PATH"]
    user = get_user_by_username(db_path, username)
    if not user:
        abort(404)

    items = get_followers(db_path, username)
    doc = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": url_for("activitypub.followers", username=username, _external=True),
        "type": "OrderedCollection",
        "totalItems": len(items),
        "orderedItems": [f["actor_url"] for f in items],
    }
    resp = jsonify(doc)
    resp.content_type = _AP_MIME
    return resp


# ---------------------------------------------------------------------------
# Stub endpoints
# ---------------------------------------------------------------------------

@bp.route("/users/<username>/outbox")
def outbox(username):
    abort(501)


@bp.route("/users/<username>/following")
def following(username):
    abort(501)
