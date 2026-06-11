"""
ActivityPub federation layer.

Registered as a Flask Blueprint in app.py.  Implements:

  /.well-known/webfinger   — account discovery (RFC 7033)
  /users/<username>        — Actor document (JSON-LD) or public HTML profile

Subsequent layers (inbox, outbox, delivery) will live here too.
"""

from flask import (
    Blueprint, abort, current_app, jsonify, redirect,
    render_template, request, url_for,
)
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

from database import get_user_by_username
from database import _conn as _db_conn

bp = Blueprint("activitypub", __name__)

_AP_MIME  = "application/activity+json"
_JRD_MIME = "application/jrd+json"

_AP_CONTEXT = [
    "https://www.w3.org/ns/activitystreams",
    "https://w3id.org/security/v1",
]


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

    resp = jsonify({
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
    })
    resp.content_type = _AP_MIME
    return resp


# ---------------------------------------------------------------------------
# Stub endpoints — needed for url_for() in the actor document
# ---------------------------------------------------------------------------

@bp.route("/users/<username>/inbox", methods=["GET", "POST"])
def inbox(username):
    abort(501)  # Not Implemented yet


@bp.route("/users/<username>/outbox")
def outbox(username):
    abort(501)


@bp.route("/users/<username>/followers")
def followers(username):
    abort(501)


@bp.route("/users/<username>/following")
def following(username):
    abort(501)
