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
import threading
import time
import nh3
from datetime import datetime, timezone, timedelta
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
    add_following, accept_following, remove_following,
    enqueue_delivery, get_due_deliveries,
    mark_delivery_sent, update_delivery_attempt, mark_delivery_failed,
    count_activities, list_activities,
    get_nodeinfo_stats,
    add_feed_item,
    add_reaction, remove_reaction,
    delete_feed_item,
    update_following_profile,
    get_following as _db_get_following,
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
    try:
        row = conn.execute(
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
    finally:
        conn.close()


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
    doc = _build_actor_doc(username, user, pub_pem)
    resp = jsonify(doc)
    resp.content_type = _AP_MIME
    return resp


def _build_actor_doc(username: str, user, pub_pem: str) -> dict:
    """Build the Actor JSON-LD document for a user. Requires an active request context."""
    actor_url  = url_for("activitypub.actor",    username=username, _external=True)
    inbox_url  = url_for("activitypub.inbox",     username=username, _external=True)
    outbox_url = url_for("activitypub.outbox",    username=username, _external=True)
    foll_url   = url_for("activitypub.followers", username=username, _external=True)
    fing_url   = url_for("activitypub.following", username=username, _external=True)
    avatar_url = url_for("user_avatar",           username=username, _external=True)

    doc = {
        "@context":          _AP_CONTEXT,
        "id":                actor_url,
        "type":              "Person",
        "preferredUsername": username,
        "inbox":             inbox_url,
        "outbox":            outbox_url,
        "followers":         foll_url,
        "following":         fing_url,
        "endpoints":         {"sharedInbox": url_for("activitypub.shared_inbox", _external=True)},
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

    return doc


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

@bp.route("/inbox", methods=["POST"])
def shared_inbox():
    db_path = current_app.config["DB_PATH"]
    raw = request.get_data()

    ok, reason = _verify_http_signature(request, raw)
    if not ok:
        _log.warning("Shared inbox: rejected — %s", reason)
        abort(401)

    try:
        activity = json.loads(raw)
        if not isinstance(activity, dict):
            abort(400)
    except (ValueError, TypeError):
        abort(400)

    # Determine which local users this activity is addressed to
    from database import get_all_users as _get_all_users
    all_users = _get_all_users(db_path)
    addressed = set()
    for field in ("to", "cc"):
        val = activity.get(field, [])
        if isinstance(val, str):
            val = [val]
        for url in val:
            for u in all_users:
                actor_url = url_for("activitypub.actor", username=u["username"], _external=True)
                if url in (actor_url, f"{actor_url}/followers"):
                    addressed.add(u["username"])

    # Fall back to all local users (e.g. public activities with no explicit addressing)
    targets = addressed or {u["username"] for u in all_users}

    for username in targets:
        user = get_user_by_username(db_path, username)
        if user:
            _dispatch_inbox(username, user, activity, db_path)

    return "", 202


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

    _dispatch_inbox(username, user, activity, db_path)
    return "", 202


def _dispatch_inbox(username, user, activity, db_path):
    activity_type = activity.get("type")
    if activity_type == "Follow":
        _handle_follow(username, user, activity, db_path)
    elif activity_type == "Accept":
        obj = activity.get("object", {})
        if isinstance(obj, dict) and obj.get("type") == "Follow":
            remote_actor = activity.get("actor", "")
            if remote_actor:
                accept_following(db_path, username, remote_actor)
    elif activity_type == "Create":
        obj = activity.get("object", {})
        if isinstance(obj, dict) and obj.get("type") == "Note":
            _handle_create_note(username, activity, obj, db_path)
    elif activity_type == "Update":
        obj = activity.get("object", {})
        if isinstance(obj, dict) and obj.get("type") in (
                "Person", "Service", "Application", "Group", "Organization"):
            _handle_update_person(username, activity, obj, db_path)
    elif activity_type == "Delete":
        _handle_delete_note(username, activity, db_path)
    elif activity_type == "Like":
        _handle_like(username, activity, db_path)
    elif activity_type == "Announce":
        _handle_announce(username, activity, db_path)
    elif activity_type == "Undo":
        obj = activity.get("object", {})
        if isinstance(obj, dict):
            if obj.get("type") == "Follow":
                _handle_undo_follow(username, activity, db_path)
            elif obj.get("type") == "Like":
                _handle_undo_reaction(username, obj, db_path, "like")
            elif obj.get("type") == "Announce":
                _handle_undo_reaction(username, obj, db_path, "boost")


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
    _deliver_activity(inbox_url, accept, key_id, db_path)


def _handle_undo_follow(local_username, activity, db_path):
    actor_url = activity.get("actor")
    if actor_url:
        remove_follower(db_path, local_username, actor_url)


def _handle_create_note(local_username, activity, note_obj, db_path):
    actor_url = activity.get("actor", "")
    if not actor_url:
        return

    following = _db_get_following(db_path, local_username)
    known_actors = {f["actor_url"] for f in following}
    is_following = actor_url in known_actors

    # Accept notes from followed accounts, or replies to our own posts
    if not is_following:
        in_reply_to = note_obj.get("inReplyTo")
        if not in_reply_to:
            return
        actor_base = url_for("activitypub.actor", username=local_username, _external=True)
        replies_to_us = (
            in_reply_to.startswith(f"{actor_base}/activities/")
            if isinstance(in_reply_to, str)
            else any(r.startswith(f"{actor_base}/activities/")
                     for r in in_reply_to if isinstance(r, str))
        )
        if not replies_to_us:
            return

    actor_info = next((f for f in following if f["actor_url"] == actor_url), {})
    actor_name   = actor_info.get("display_name") or ""
    actor_avatar = actor_info.get("avatar_url") or ""

    # For unknown actors (repliers we don't follow), fetch their profile
    if not is_following:
        try:
            resp = requests.get(actor_url, headers={"Accept": _AP_MIME}, timeout=5)
            if resp.ok:
                doc = resp.json()
                actor_name   = doc.get("name") or doc.get("preferredUsername") or actor_url
                icon = doc.get("icon", {})
                actor_avatar = icon.get("url", "") if isinstance(icon, dict) else ""
        except Exception:
            pass
    if not actor_name:
        actor_name = actor_url

    object_id   = note_obj.get("id", "")
    object_url  = note_obj.get("url") or object_id
    in_reply_to = note_obj.get("inReplyTo")
    if isinstance(in_reply_to, list):
        in_reply_to = in_reply_to[0] if in_reply_to else None
    raw_content = note_obj.get("content", "") or ""
    content = nh3.clean(
        raw_content,
        tags={"p", "br", "a", "strong", "em", "b", "i", "ul", "ol", "li",
              "blockquote", "code", "pre", "span"},
        attributes={"a": {"href", "rel", "class"}, "span": {"class"}},
        link_rel=None,
    )
    published  = note_obj.get("published", "")
    if in_reply_to:
        content = (f'<p style="color:var(--pico-muted-color);font-size:.85rem">'
                   f'↩ <a href="{in_reply_to}" target="_blank" rel="noopener">In reply to</a>'
                   f'</p>') + content

    raw_attachments = note_obj.get("attachment") or []
    if isinstance(raw_attachments, dict):
        raw_attachments = [raw_attachments]
    attachments = [
        {"url": a.get("url", ""), "mediaType": a.get("mediaType", ""), "type": a.get("type", "")}
        for a in raw_attachments if isinstance(a, dict) and a.get("url")
    ]

    add_feed_item(
        db_path, local_username, actor_url, actor_name, actor_avatar,
        object_id, object_url, content, published,
        json.dumps(attachments) if attachments else None,
    )


def _handle_update_person(local_username: str, activity: dict, obj: dict, db_path: str):
    actor_url = activity.get("actor", "")
    # Only update if the actor is updating their own profile
    if not actor_url or obj.get("id") != actor_url:
        return
    # Only update accounts we actually follow
    following = _db_get_following(db_path, local_username)
    if not any(f["actor_url"] == actor_url for f in following):
        return
    display_name = obj.get("name") or obj.get("preferredUsername") or ""
    icon = obj.get("icon", {})
    avatar_url = icon.get("url", "") if isinstance(icon, dict) else ""
    update_following_profile(db_path, local_username, actor_url,
                             display_name or None, avatar_url or None)
    _log.info("Updated profile for %s", actor_url)


def _handle_delete_note(local_username: str, activity: dict, db_path: str):
    actor_url = activity.get("actor", "")
    obj = activity.get("object", "")
    object_id = obj if isinstance(obj, str) else (obj.get("id", "") if isinstance(obj, dict) else "")
    if actor_url and object_id:
        delete_feed_item(db_path, local_username, object_id, actor_url)
        _log.info("Deleted feed item %s from %s", object_id, actor_url)


def _activity_id_from_note_url(note_url: str, local_actor_base: str) -> int | None:
    """Extract the numeric activity ID from a bikeodon note URL, or None if not ours."""
    prefix = f"{local_actor_base}/activities/"
    if not note_url.startswith(prefix):
        return None
    try:
        return int(note_url[len(prefix):].split("/")[0])
    except (ValueError, IndexError):
        return None


def _handle_like(local_username: str, activity: dict, db_path: str):
    actor_url = activity.get("actor", "")
    obj = activity.get("object", "")
    note_url = obj if isinstance(obj, str) else (obj.get("id", "") if isinstance(obj, dict) else "")
    if not actor_url or not note_url:
        return
    actor_base = url_for("activitypub.actor", username=local_username, _external=True)
    activity_id = _activity_id_from_note_url(note_url, actor_base)
    if activity_id:
        add_reaction(db_path, activity_id, actor_url, "like")
        _log.info("Like on activity %s from %s", activity_id, actor_url)


def _handle_announce(local_username: str, activity: dict, db_path: str):
    actor_url = activity.get("actor", "")
    obj = activity.get("object", "")
    note_url = obj if isinstance(obj, str) else (obj.get("id", "") if isinstance(obj, dict) else "")
    if not actor_url or not note_url:
        return
    actor_base = url_for("activitypub.actor", username=local_username, _external=True)
    activity_id = _activity_id_from_note_url(note_url, actor_base)
    if activity_id:
        add_reaction(db_path, activity_id, actor_url, "boost")
        _log.info("Boost on activity %s from %s", activity_id, actor_url)


def _handle_undo_reaction(local_username: str, obj: dict, db_path: str, reaction_type: str):
    actor_url = obj.get("actor", "")
    target = obj.get("object", "")
    note_url = target if isinstance(target, str) else (target.get("id", "") if isinstance(target, dict) else "")
    if not actor_url or not note_url:
        return
    actor_base = url_for("activitypub.actor", username=local_username, _external=True)
    activity_id = _activity_id_from_note_url(note_url, actor_base)
    if activity_id:
        remove_reaction(db_path, activity_id, actor_url, reaction_type)
        _log.info("Undo %s on activity %s from %s", reaction_type, activity_id, actor_url)


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
        _deliver_activity(inbox_url, follow_activity, key_id, db_path)


def send_unfollow(local_username: str, local_user, remote_actor_url: str, db_path: str):
    """Send Undo{Follow} to the remote actor and remove from the following table."""
    from database import get_following as _get_following
    rows = _get_following(db_path, local_username)
    row  = next((r for r in rows if r["actor_url"] == remote_actor_url), None)
    if not row:
        return

    actor_ap_url = url_for("activitypub.actor", username=local_username, _external=True)
    _, priv_pem  = get_or_create_keypair(db_path, local_user["id"])
    key_id       = f"{actor_ap_url}#main-key"

    follow_id = f"{actor_ap_url}#follow/{abs(hash(remote_actor_url)):08x}"
    undo = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id":       f"{follow_id}/undo",
        "type":     "Undo",
        "actor":    actor_ap_url,
        "object": {
            "id":     follow_id,
            "type":   "Follow",
            "actor":  actor_ap_url,
            "object": remote_actor_url,
        },
    }

    inbox_url = row.get("inbox_url")
    if inbox_url:
        _deliver_activity(inbox_url, undo, key_id, db_path)

    remove_following(db_path, local_username, remote_actor_url)


def send_profile_update(local_username: str, local_user, db_path: str):
    """Fan out an Update{Person} to all followers after a profile change."""
    followers = get_followers(db_path, local_username)
    if not followers:
        return

    pub_pem, _ = get_or_create_keypair(db_path, local_user["id"])
    actor_doc  = _build_actor_doc(local_username, local_user, pub_pem)
    actor_url  = actor_doc["id"]
    key_id     = f"{actor_url}#main-key"

    update = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id":       f"{actor_url}#update/{int(datetime.now(timezone.utc).timestamp())}",
        "type":     "Update",
        "actor":    actor_url,
        "to":       ["https://www.w3.org/ns/activitystreams#Public"],
        "object":   actor_doc,
    }

    for follower in followers:
        inbox_url = follower.get("inbox_url")
        if inbox_url:
            _deliver_activity(inbox_url, update, key_id, db_path)

    _log.info("Queued profile Update for %s to %d follower(s)", local_username, len(followers))


def _resolve_inbox(actor_url: str, db_path: str, local_username: str) -> str | None:
    """Return inbox URL for actor_url: check following table first, then fetch actor doc."""
    following = _db_get_following(db_path, local_username)
    match = next((f for f in following if f["actor_url"] == actor_url), None)
    if match and match.get("inbox_url"):
        return match["inbox_url"]
    try:
        resp = requests.get(actor_url, headers={"Accept": _AP_MIME}, timeout=5)
        if resp.ok:
            return resp.json().get("inbox")
    except Exception:
        pass
    return None


def send_reply(local_username: str, local_user, object_id: str, actor_url: str,
               content: str, db_path: str):
    """Send a Create{Note} reply to object_id from local_username."""
    inbox_url = _resolve_inbox(actor_url, db_path, local_username)
    if not inbox_url:
        _log.warning("send_reply: could not resolve inbox for %s", actor_url)
        return

    actor_ap_url = url_for("activitypub.actor", username=local_username, _external=True)
    _, priv_pem = get_or_create_keypair(db_path, local_user["id"])
    key_id = f"{actor_ap_url}#main-key"
    followers_url = f"{actor_ap_url}/followers"

    published = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    reply_id = f"{actor_ap_url}/replies/{abs(hash(object_id + content + published)):016x}"

    # Wrap plain text in paragraph tags, preserving line breaks
    safe = nh3.clean(content, tags={"p", "br", "a", "strong", "em"}, link_rel=None)
    if not safe:
        safe = "<p>" + content.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"

    note = {
        "id": reply_id,
        "type": "Note",
        "attributedTo": actor_ap_url,
        "inReplyTo": object_id,
        "content": safe,
        "published": published,
        "to": ["https://www.w3.org/ns/activitystreams#Public"],
        "cc": [followers_url, actor_url],
    }
    create = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{reply_id}/create",
        "type": "Create",
        "actor": actor_ap_url,
        "published": published,
        "to": ["https://www.w3.org/ns/activitystreams#Public"],
        "cc": [followers_url, actor_url],
        "object": note,
    }
    _deliver_activity(inbox_url, create, key_id, db_path)


def _is_local_actor(actor_url: str) -> bool:
    """True if actor_url belongs to this server — no federation needed."""
    local_base = url_for("activitypub.actor", username="_", _external=True).rsplit("/_", 1)[0]
    return actor_url.startswith(local_base)


def send_like(local_username: str, local_user, object_id: str, actor_url: str, db_path: str):
    if _is_local_actor(actor_url):
        return
    inbox_url = _resolve_inbox(actor_url, db_path, local_username)
    if not inbox_url:
        return
    actor_ap_url = url_for("activitypub.actor", username=local_username, _external=True)
    _, priv_pem = get_or_create_keypair(db_path, local_user["id"])
    key_id = f"{actor_ap_url}#main-key"
    like_id = f"{actor_ap_url}/likes/{abs(hash(object_id)):016x}"
    activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id":     like_id,
        "type":   "Like",
        "actor":  actor_ap_url,
        "object": object_id,
    }
    _deliver_activity(inbox_url, activity, key_id, db_path)


def send_unlike(local_username: str, local_user, object_id: str, actor_url: str, db_path: str):
    if _is_local_actor(actor_url):
        return
    inbox_url = _resolve_inbox(actor_url, db_path, local_username)
    if not inbox_url:
        return
    actor_ap_url = url_for("activitypub.actor", username=local_username, _external=True)
    _, priv_pem = get_or_create_keypair(db_path, local_user["id"])
    key_id = f"{actor_ap_url}#main-key"
    like_id = f"{actor_ap_url}/likes/{abs(hash(object_id)):016x}"
    activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id":     f"{like_id}/undo",
        "type":   "Undo",
        "actor":  actor_ap_url,
        "object": {"id": like_id, "type": "Like", "actor": actor_ap_url, "object": object_id},
    }
    _deliver_activity(inbox_url, activity, key_id, db_path)


def send_boost(local_username: str, local_user, object_id: str, actor_url: str, db_path: str):
    if _is_local_actor(actor_url):
        return
    inbox_url = _resolve_inbox(actor_url, db_path, local_username)
    if not inbox_url:
        return
    actor_ap_url = url_for("activitypub.actor", username=local_username, _external=True)
    _, priv_pem = get_or_create_keypair(db_path, local_user["id"])
    key_id = f"{actor_ap_url}#main-key"
    boost_id = f"{actor_ap_url}/boosts/{abs(hash(object_id)):016x}"
    followers_url = f"{actor_ap_url}/followers"
    activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id":      boost_id,
        "type":    "Announce",
        "actor":   actor_ap_url,
        "object":  object_id,
        "to":      ["https://www.w3.org/ns/activitystreams#Public"],
        "cc":      [followers_url],
    }
    _deliver_activity(inbox_url, activity, key_id, db_path)


def send_unboost(local_username: str, local_user, object_id: str, actor_url: str, db_path: str):
    if _is_local_actor(actor_url):
        return
    inbox_url = _resolve_inbox(actor_url, db_path, local_username)
    if not inbox_url:
        return
    actor_ap_url = url_for("activitypub.actor", username=local_username, _external=True)
    _, priv_pem = get_or_create_keypair(db_path, local_user["id"])
    key_id = f"{actor_ap_url}#main-key"
    boost_id = f"{actor_ap_url}/boosts/{abs(hash(object_id)):016x}"
    activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id":     f"{boost_id}/undo",
        "type":   "Undo",
        "actor":  actor_ap_url,
        "object": {"id": boost_id, "type": "Announce", "actor": actor_ap_url, "object": object_id},
    }
    _deliver_activity(inbox_url, activity, key_id, db_path)


def _deliver_activity(inbox_url, activity_doc, key_id, db_path):
    """Enqueue an activity for async delivery. Returns immediately."""
    enqueue_delivery(db_path, inbox_url, json.dumps(activity_doc), key_id)


def _do_http_post(inbox_url, body_bytes, priv_pem, key_id):
    """Attempt a single signed HTTP POST. Raises on non-2xx (except 410)."""
    hdrs = _sign_headers("POST", inbox_url, body_bytes, priv_pem, key_id)
    r = requests.post(inbox_url, data=body_bytes, headers=hdrs, timeout=15)
    if r.status_code == 410:
        return  # Gone — treat as success, stop retrying
    if not r.ok:
        raise Exception(f"HTTP {r.status_code}: {r.text[:200]}")


_BACKOFF_INITIAL = 5 * 60       # 5 minutes
_BACKOFF_MAX     = 6 * 3600     # 6 hours
_DELIVERY_TTL    = 3 * 24 * 3600  # 3 days


def _process_due_deliveries(db_path):
    """Deliver all due queue items. Called by the worker thread."""
    rows = get_due_deliveries(db_path)
    for row in rows:
        delivery_id = row["id"]
        attempts    = row["attempts"]
        created_at  = row["created_at"]
        key_id      = row["key_id"]
        inbox_url   = row["inbox_url"]
        body_bytes  = row["activity_json"].encode()

        # Check if this delivery has aged out (> 3 days old)
        try:
            age = (datetime.now(timezone.utc) -
                   datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                   ).total_seconds()
        except Exception:
            age = 0

        # Extract username from key_id to look up the private key
        try:
            username = key_id.split("/users/")[1].split("#")[0]
            user = get_user_by_username(db_path, username)
            if not user:
                raise Exception(f"user {username!r} not found")
            _, priv_pem = get_or_create_keypair(db_path, user["id"])
            _do_http_post(inbox_url, body_bytes, priv_pem, key_id)
            mark_delivery_sent(db_path, delivery_id)
            _log.info("Delivered to %s (attempt %d)", inbox_url, attempts + 1)
        except Exception as exc:
            error = str(exc)
            new_attempts = attempts + 1
            if age > _DELIVERY_TTL:
                mark_delivery_failed(db_path, delivery_id, error)
                _log.warning("Giving up on %s after %d attempts (%.0fh old): %s",
                             inbox_url, new_attempts, age / 3600, error)
            else:
                backoff = min(_BACKOFF_INITIAL * (2 ** attempts), _BACKOFF_MAX)
                next_at = (datetime.now(timezone.utc) + timedelta(seconds=backoff)).isoformat()
                update_delivery_attempt(db_path, delivery_id, next_at, new_attempts, error)
                _log.warning("Delivery to %s failed (attempt %d, retry in %ds): %s",
                             inbox_url, new_attempts, backoff, error)


def start_delivery_worker(db_path):
    """Start the background delivery thread. Safe to call multiple times."""
    def _loop():
        while True:
            try:
                _process_due_deliveries(db_path)
            except Exception as exc:
                _log.error("Delivery worker error: %s", exc)
            time.sleep(30)

    t = threading.Thread(target=_loop, daemon=True, name="delivery-worker")
    t.start()
    _log.info("Delivery worker started")


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
# Outbox  GET /users/<username>/outbox[?page=true]
# ---------------------------------------------------------------------------

_OUTBOX_PAGE_SIZE = 20

_SPORT_HASHTAGS: dict[str, list[str]] = {
    "Ride":           ["cycling"],
    "VirtualRide":    ["cycling", "zwift"],
    "Run":            ["running"],
    "VirtualRun":     ["running"],
    "Walk":           ["walking"],
    "Hike":           ["hiking"],
    "Swim":           ["swimming"],
    "WeightTraining": ["weighttraining", "fitness"],
    "Yoga":           ["yoga", "fitness"],
    "Workout":        ["fitness"],
    "Rowing":         ["rowing"],
    "Kayaking":       ["kayaking"],
    "AlpineSki":      ["skiing"],
    "NordicSki":      ["nordicski"],
    "Snowboard":      ["snowboarding"],
}
_BASE_HASHTAGS = ["strava", "bikeodon"]


def _hashtags_for_activity(sport_type: str | None) -> list[str]:
    """Return ordered list of hashtag names (without #) for an activity."""
    tags = list(_SPORT_HASHTAGS.get(sport_type or "", []))
    for t in _BASE_HASHTAGS:
        if t not in tags:
            tags.append(t)
    return tags


def _activity_row_to_ap(row, actor_url: str, outbox_url: str,
                        image_urls: list[str] | None = None) -> dict:
    """Convert a DB activity row to an ActivityPub Create{Note} activity."""
    row = dict(row)
    note_id = f"{actor_url}/activities/{row['id']}"

    # Derive the base URL from actor_url (e.g. https://bikeodon.org)
    from urllib.parse import urlparse as _urlparse
    base_url = "{0.scheme}://{0.netloc}".format(_urlparse(actor_url))

    content_parts = [f"<p>{row.get('name', 'Activity')}</p>"]
    dist_m = row.get("distance") or 0
    elev_m = row.get("total_elevation_gain") or 0
    if dist_m:
        content_parts.append(f"<p>📍 {dist_m/1000:.1f} km  🏔 {elev_m:.0f} m</p>")

    hashtag_names = _hashtags_for_activity(row.get("sport_type"))
    tag_links = " ".join(
        f'<a href="{base_url}/tags/{t}" class="mention hashtag" rel="tag">#<span>{t}</span></a>'
        for t in hashtag_names
    )
    content_parts.append(f"<p>{tag_links}</p>")
    content = "".join(content_parts)

    tag_objects = [
        {"type": "Hashtag", "href": f"{base_url}/tags/{t}", "name": f"#{t}"}
        for t in hashtag_names
    ]

    published = row.get("start_date") or row.get("fetched_at") or ""
    if published and not published.endswith("Z") and "+" not in published:
        published = published.replace(" ", "T") + "Z"

    attachments = [
        {"type": "Document", "mediaType": "image/png", "url": u}
        for u in (image_urls or [])
    ]

    followers_url = f"{actor_url}/followers"

    note = {
        "id": note_id,
        "type": "Note",
        "url": f"{base_url}/activity/{row['id']}",
        "attributedTo": actor_url,
        "content": content,
        "tag": tag_objects,
        "published": published,
        "to": ["https://www.w3.org/ns/activitystreams#Public"],
        "cc": [followers_url],
    }
    if attachments:
        note["attachment"] = attachments

    return {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{note_id}/create",
        "type": "Create",
        "actor": actor_url,
        "published": published,
        "to": ["https://www.w3.org/ns/activitystreams#Public"],
        "cc": [followers_url],
        "object": note,
    }


@bp.route("/users/<username>/outbox")
def outbox(username):
    db_path = current_app.config["DB_PATH"]
    user = get_user_by_username(db_path, username)
    if not user:
        abort(404)

    actor_url  = url_for("activitypub.actor",  username=username, _external=True)
    outbox_url = url_for("activitypub.outbox", username=username, _external=True)
    total      = count_activities(db_path, user["id"])

    if request.args.get("page") == "true":
        offset = int(request.args.get("min_id", 0))
        rows   = list_activities(db_path, user["id"], limit=_OUTBOX_PAGE_SIZE, offset=offset)
        items  = [_activity_row_to_ap(r, actor_url, outbox_url) for r in rows]
        doc = {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": f"{outbox_url}?page=true",
            "type": "OrderedCollectionPage",
            "partOf": outbox_url,
            "totalItems": total,
            "orderedItems": items,
        }
    else:
        doc = {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": outbox_url,
            "type": "OrderedCollection",
            "totalItems": total,
            "first": f"{outbox_url}?page=true",
        }

    resp = jsonify(doc)
    resp.content_type = _AP_MIME
    return resp


# ---------------------------------------------------------------------------
# Individual activity  GET /users/<username>/activities/<activity_id>
# ---------------------------------------------------------------------------

@bp.route("/users/<username>/activities/<int:activity_id>")
def activity_object(username, activity_id):
    db_path = current_app.config["DB_PATH"]
    user = get_user_by_username(db_path, username)
    if not user:
        abort(404)

    from database import get_activity as _get_activity
    row = _get_activity(db_path, activity_id, user_id=user["id"])
    if not row:
        abort(404)

    actor_url  = url_for("activitypub.actor",  username=username, _external=True)
    outbox_url = url_for("activitypub.outbox", username=username, _external=True)

    out_dir = current_app.config.get("OUTPUT_DIR", "output")
    import os as _os
    image_urls = [
        url_for("output_file", filename=f"{activity_id}{s}.png", _external=True)
        for s in ["", "_hr", "_power"]
        if _os.path.exists(_os.path.join(_os.path.abspath(out_dir), f"{activity_id}{s}.png"))
    ]

    create_activity = _activity_row_to_ap(row, actor_url, outbox_url, image_urls=image_urls)
    note = create_activity["object"]

    resp = jsonify(note)
    resp.content_type = _AP_MIME
    return resp


# ---------------------------------------------------------------------------
# Following  GET /users/<username>/following
# ---------------------------------------------------------------------------

@bp.route("/users/<username>/following")
def following(username):
    from database import get_following as _get_following
    db_path = current_app.config["DB_PATH"]
    user = get_user_by_username(db_path, username)
    if not user:
        abort(404)

    items = _get_following(db_path, username)
    accepted = [f["actor_url"] for f in items if f.get("status") == "accepted"]
    doc = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": url_for("activitypub.following", username=username, _external=True),
        "type": "OrderedCollection",
        "totalItems": len(accepted),
        "orderedItems": accepted,
    }
    resp = jsonify(doc)
    resp.content_type = _AP_MIME
    return resp


# ---------------------------------------------------------------------------
# NodeInfo  /.well-known/nodeinfo  +  /nodeinfo/2.0
# ---------------------------------------------------------------------------

@bp.route("/.well-known/nodeinfo")
def nodeinfo_discovery():
    href = url_for("activitypub.nodeinfo", _external=True)
    doc = {
        "links": [
            {
                "rel": "http://nodeinfo.diaspora.software/ns/schema/2.0",
                "href": href,
            }
        ]
    }
    return jsonify(doc)


@bp.route("/nodeinfo/2.0")
def nodeinfo():
    db_path = current_app.config["DB_PATH"]
    stats   = get_nodeinfo_stats(db_path)
    doc = {
        "version": "2.0",
        "software": {
            "name":    "bikeodon",
            "version": "0.1",
        },
        "protocols": ["activitypub"],
        "usage": {
            "users": {
                "total":          stats["user_count"],
                "activeHalfyear": stats["active_halfyear"],
                "activeMonth":    stats["active_month"],
            },
            "localPosts": stats["local_posts"],
        },
        "openRegistrations": False,
        "services": {"inbound": [], "outbound": []},
        "metadata": {},
    }
    return jsonify(doc)
