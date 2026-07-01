"""
Tests for new database functions:
  - delete_activity (cascade to cp_history, activity_reactions, feed_items, local_reactions)
  - get_feed_replies (grouping by parent object_id)
  - clear_ap_posted
  - add_feed_item with in_reply_to
"""

import os
import pytest
import yaml
from werkzeug.security import generate_password_hash


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("cascade")
    path = str(tmp / "test.db")
    cfg = {
        "database": {"path": path},
        "map":      {"output_dir": str(tmp / "output"), "tiles": {}},
    }
    with open(str(tmp / "config.yaml"), "w") as f:
        yaml.dump(cfg, f)
    os.environ["BIKEODON_CONFIG"] = str(tmp / "config.yaml")
    os.environ["FLASK_SECRET_KEY"] = "test-secret"

    import importlib, database
    importlib.reload(database)
    database.init_db(path)
    return path


@pytest.fixture(scope="module")
def uid(db_path):
    from database import create_user
    return create_user(db_path, "cascade_user", generate_password_hash("pw"))


@pytest.fixture(scope="module")
def other_uid(db_path):
    from database import create_user
    return create_user(db_path, "other_user", generate_password_hash("pw"))


def _insert_activity(db_path, uid, activity_id, name="Test Ride"):
    from database import upsert_activity
    upsert_activity(db_path, {
        "id": activity_id,
        "name": name,
        "sport_type": "Ride",
        "start_date": "2026-01-15T10:00:00Z",
        "distance": 20000.0,
        "moving_time": 3600,
        "elapsed_time": 3700,
    }, user_id=uid)


def _conn(db_path):
    from database import _conn as _db_conn
    return _db_conn(db_path)


# ---------------------------------------------------------------------------
# TestDeleteActivity
# ---------------------------------------------------------------------------

class TestDeleteActivity:

    AID = 70001

    def setup_method(self):
        pass

    def _seed(self, db_path, uid, activity_id=None):
        aid = activity_id or self.AID
        _insert_activity(db_path, uid, aid)
        c = _conn(db_path)
        # seed cp_history
        c.execute(
            "INSERT OR REPLACE INTO cp_history"
            " (user_id, activity_id, activity_date, cp_watts, w_prime_joules, basis_activities)"
            " VALUES (?,?,?,?,?,?)",
            (uid, aid, "2026-01-15", 230.0, 18000.0, 5),
        )
        # seed activity_reactions
        c.execute(
            "INSERT OR IGNORE INTO activity_reactions (activity_id, actor_url, type)"
            " VALUES (?,?,?)",
            (aid, "https://remote.example/users/bob", "like"),
        )
        c.commit()
        c.close()

    def test_returns_deleted_row(self, db_path, uid):
        self._seed(db_path, uid)
        from database import delete_activity
        row = delete_activity(db_path, self.AID, uid)
        assert row is not None
        assert row["id"] == self.AID

    def test_removes_activity_row(self, db_path, uid):
        self._seed(db_path, uid)
        from database import delete_activity, get_activity
        delete_activity(db_path, self.AID, uid)
        assert get_activity(db_path, self.AID, uid) is None

    def test_cascades_to_cp_history(self, db_path, uid):
        self._seed(db_path, uid)
        from database import delete_activity
        delete_activity(db_path, self.AID, uid)
        c = _conn(db_path)
        row = c.execute(
            "SELECT 1 FROM cp_history WHERE activity_id=? AND user_id=?", (self.AID, uid)
        ).fetchone()
        c.close()
        assert row is None

    def test_cascades_to_activity_reactions(self, db_path, uid):
        self._seed(db_path, uid)
        from database import delete_activity
        delete_activity(db_path, self.AID, uid)
        c = _conn(db_path)
        row = c.execute(
            "SELECT 1 FROM activity_reactions WHERE activity_id=?", (self.AID,)
        ).fetchone()
        c.close()
        assert row is None

    def test_cascades_to_feed_items(self, db_path, uid, other_uid):
        self._seed(db_path, uid)
        note_id = f"https://bikeodon.org/users/cascade_user/activities/{self.AID}"
        from database import add_feed_item, delete_activity
        add_feed_item(db_path, "cascade_user",
                      "https://bikeodon.org/users/cascade_user",
                      "Cascade User", None, note_id, note_id,
                      "<p>ride</p>", "2026-01-15T10:00:00Z", None)
        delete_activity(db_path, self.AID, uid, note_id=note_id, username="cascade_user")
        c = _conn(db_path)
        row = c.execute(
            "SELECT 1 FROM feed_items WHERE object_id=?", (note_id,)
        ).fetchone()
        c.close()
        assert row is None

    def test_cascades_to_local_reactions(self, db_path, uid):
        self._seed(db_path, uid)
        note_id = f"https://bikeodon.org/users/cascade_user/activities/{self.AID}"
        from database import add_local_reaction, delete_activity
        add_local_reaction(db_path, "cascade_user", note_id, "like")
        delete_activity(db_path, self.AID, uid, note_id=note_id, username="cascade_user")
        c = _conn(db_path)
        row = c.execute(
            "SELECT 1 FROM local_reactions WHERE object_id=?", (note_id,)
        ).fetchone()
        c.close()
        assert row is None

    def test_returns_none_for_wrong_user(self, db_path, uid, other_uid):
        _insert_activity(db_path, uid, 70002)
        from database import delete_activity
        result = delete_activity(db_path, 70002, other_uid)
        assert result is None

    def test_does_not_delete_other_users_activity(self, db_path, uid, other_uid):
        _insert_activity(db_path, uid, 70003)
        from database import delete_activity, get_activity
        delete_activity(db_path, 70003, other_uid)  # wrong user — should no-op
        assert get_activity(db_path, 70003, uid) is not None

    def test_does_not_affect_sibling_activity(self, db_path, uid):
        _insert_activity(db_path, uid, 70010)
        _insert_activity(db_path, uid, 70011)
        from database import delete_activity, get_activity
        delete_activity(db_path, 70010, uid)
        assert get_activity(db_path, 70011, uid) is not None

    def test_cp_history_of_other_activity_survives(self, db_path, uid):
        _insert_activity(db_path, uid, 70020)
        _insert_activity(db_path, uid, 70021)
        c = _conn(db_path)
        c.execute(
            "INSERT OR REPLACE INTO cp_history"
            " (user_id, activity_id, activity_date, cp_watts, w_prime_joules, basis_activities)"
            " VALUES (?,?,?,?,?,?)",
            (uid, 70021, "2026-01-16", 235.0, 18500.0, 6),
        )
        c.commit()
        c.close()
        from database import delete_activity
        delete_activity(db_path, 70020, uid)
        c = _conn(db_path)
        row = c.execute(
            "SELECT 1 FROM cp_history WHERE activity_id=70021 AND user_id=?", (uid,)
        ).fetchone()
        c.close()
        assert row is not None


# ---------------------------------------------------------------------------
# TestGetFeedReplies
# ---------------------------------------------------------------------------

class TestGetFeedReplies:

    def _add_item(self, db_path, username, object_id, content, published,
                  in_reply_to=None):
        from database import add_feed_item
        add_feed_item(
            db_path, username,
            f"https://bikeodon.org/users/{username}",
            username, None, object_id, object_id,
            content, published, None,
            in_reply_to=in_reply_to,
        )

    def test_empty_ids_returns_empty_dict(self, db_path):
        from database import get_feed_replies
        assert get_feed_replies(db_path, "cascade_user", []) == {}

    def test_no_replies_returns_empty_dict(self, db_path):
        from database import get_feed_replies
        self._add_item(db_path, "cascade_user",
                       "https://remote.example/post/standalone",
                       "<p>post</p>", "2026-02-01T10:00:00Z")
        result = get_feed_replies(db_path, "cascade_user",
                                  ["https://remote.example/post/standalone"])
        assert result == {}

    def test_groups_reply_under_parent(self, db_path):
        from database import get_feed_replies
        parent_id = "https://remote.example/post/p1"
        reply_id  = "https://bikeodon.org/users/cascade_user/replies/r1"
        self._add_item(db_path, "cascade_user", parent_id,
                       "<p>parent</p>", "2026-02-02T10:00:00Z")
        self._add_item(db_path, "cascade_user", reply_id,
                       "<p>reply</p>", "2026-02-02T10:05:00Z",
                       in_reply_to=parent_id)
        result = get_feed_replies(db_path, "cascade_user", [parent_id])
        assert parent_id in result
        assert len(result[parent_id]) == 1
        assert result[parent_id][0]["object_id"] == reply_id

    def test_ignores_replies_to_other_parents(self, db_path):
        from database import get_feed_replies
        known_parent   = "https://remote.example/post/known"
        unknown_parent = "https://remote.example/post/unknown"
        self._add_item(db_path, "cascade_user",
                       "https://bikeodon.org/users/cascade_user/replies/rX",
                       "<p>r</p>", "2026-02-03T10:00:00Z",
                       in_reply_to=unknown_parent)
        result = get_feed_replies(db_path, "cascade_user", [known_parent])
        assert result.get(known_parent) is None

    def test_multiple_replies_ordered_ascending(self, db_path):
        from database import get_feed_replies
        parent_id = "https://remote.example/post/p2"
        self._add_item(db_path, "cascade_user", parent_id,
                       "<p>p</p>", "2026-02-04T10:00:00Z")
        for i, pub in enumerate(["2026-02-04T10:10:00Z",
                                  "2026-02-04T10:05:00Z",
                                  "2026-02-04T10:15:00Z"]):
            self._add_item(
                db_path, "cascade_user",
                f"https://bikeodon.org/users/cascade_user/replies/ord{i}",
                f"<p>r{i}</p>", pub, in_reply_to=parent_id,
            )
        result = get_feed_replies(db_path, "cascade_user", [parent_id])
        pubs = [r["published"] for r in result[parent_id]]
        assert pubs == sorted(pubs)

    def test_does_not_leak_other_users_replies(self, db_path):
        from database import add_feed_item, get_feed_replies
        parent_id = "https://remote.example/post/p3"
        add_feed_item(
            db_path, "other_user",
            "https://bikeodon.org/users/other_user", "other", None,
            "https://bikeodon.org/users/other_user/replies/leak",
            None, "<p>leak</p>", "2026-02-05T10:00:00Z", None,
            in_reply_to=parent_id,
        )
        result = get_feed_replies(db_path, "cascade_user", [parent_id])
        assert result.get(parent_id) is None


# ---------------------------------------------------------------------------
# TestClearApPosted
# ---------------------------------------------------------------------------

class TestClearApPosted:

    def _seed_posted(self, db_path, uid, activity_id):
        _insert_activity(db_path, uid, activity_id)
        from database import mark_ap_posted
        mark_ap_posted(db_path, activity_id, uid)

    def test_clears_ap_posted_at(self, db_path, uid):
        self._seed_posted(db_path, uid, 80001)
        from database import clear_ap_posted, get_activity
        clear_ap_posted(db_path, 80001, uid)
        assert get_activity(db_path, 80001, uid)["ap_posted_at"] is None

    def test_does_not_affect_sibling(self, db_path, uid):
        self._seed_posted(db_path, uid, 80002)
        self._seed_posted(db_path, uid, 80003)
        from database import clear_ap_posted, get_activity
        clear_ap_posted(db_path, 80002, uid)
        assert get_activity(db_path, 80003, uid)["ap_posted_at"] is not None

    def test_wrong_user_is_noop(self, db_path, uid, other_uid):
        self._seed_posted(db_path, uid, 80004)
        from database import clear_ap_posted, get_activity
        clear_ap_posted(db_path, 80004, other_uid)
        assert get_activity(db_path, 80004, uid)["ap_posted_at"] is not None


# ---------------------------------------------------------------------------
# TestAddFeedItemWithReplyTo
# ---------------------------------------------------------------------------

class TestAddFeedItemWithReplyTo:

    def test_in_reply_to_stored(self, db_path):
        from database import add_feed_item
        parent = "https://remote.example/post/parent99"
        reply  = "https://bikeodon.org/users/cascade_user/replies/stored99"
        add_feed_item(
            db_path, "cascade_user",
            "https://bikeodon.org/users/cascade_user",
            "Cascade User", None, reply, reply,
            "<p>stored</p>", "2026-03-01T10:00:00Z", None,
            in_reply_to=parent,
        )
        c = _conn(db_path)
        row = c.execute(
            "SELECT in_reply_to FROM feed_items WHERE object_id=?", (reply,)
        ).fetchone()
        c.close()
        assert row["in_reply_to"] == parent

    def test_in_reply_to_defaults_to_none(self, db_path):
        from database import add_feed_item
        oid = "https://remote.example/post/toplevel99"
        add_feed_item(
            db_path, "cascade_user",
            "https://remote.example/users/alice",
            "Alice", None, oid, oid,
            "<p>top</p>", "2026-03-02T10:00:00Z", None,
        )
        c = _conn(db_path)
        row = c.execute(
            "SELECT in_reply_to FROM feed_items WHERE object_id=?", (oid,)
        ).fetchone()
        c.close()
        assert row["in_reply_to"] is None

    def test_get_feed_items_excludes_replies(self, db_path):
        """get_feed_items should not return items that have in_reply_to set."""
        from database import add_feed_item, get_feed_items
        parent = "https://remote.example/post/excl_parent"
        reply  = "https://bikeodon.org/users/cascade_user/replies/excl_reply"
        add_feed_item(
            db_path, "cascade_user",
            "https://bikeodon.org/users/cascade_user", "u", None,
            reply, reply, "<p>r</p>", "2026-03-03T10:00:00Z", None,
            in_reply_to=parent,
        )
        items = get_feed_items(db_path, "cascade_user", limit=100)
        ids = [i["object_id"] for i in items]
        assert reply not in ids


# ---------------------------------------------------------------------------
# TestDeleteActivityTombstone
#
# Regression coverage: deleting an activity used to leave no record of the
# deletion, so the next Strava sync would see the id as "unseen" and
# silently re-import it. delete_activity() now writes a tombstone row that
# was_deleted() checks; the sync loop skips tombstoned ids (tasks.py).
# ---------------------------------------------------------------------------

class TestDeleteActivityTombstone:

    AID = 70020

    def test_was_deleted_false_before_deletion(self, db_path, uid):
        from database import was_deleted
        assert was_deleted(db_path, uid, 70021) is False

    def test_was_deleted_true_after_deletion(self, db_path, uid):
        from database import delete_activity, was_deleted
        _insert_activity(db_path, uid, self.AID)
        delete_activity(db_path, self.AID, uid)
        assert was_deleted(db_path, uid, self.AID) is True

    def test_tombstone_is_scoped_to_user(self, db_path, uid, other_uid):
        from database import delete_activity, was_deleted
        aid = 70022
        _insert_activity(db_path, uid, aid)
        delete_activity(db_path, aid, uid)
        assert was_deleted(db_path, other_uid, aid) is False

    def test_deleting_nonexistent_activity_does_not_tombstone(self, db_path, uid):
        from database import delete_activity, was_deleted
        result = delete_activity(db_path, 999999999, uid)
        assert result is None
        assert was_deleted(db_path, uid, 999999999) is False

    def test_repeated_deletion_does_not_error(self, db_path, uid):
        """INSERT OR REPLACE means re-deleting (e.g. a retried request) is safe."""
        from database import delete_activity, was_deleted
        aid = 70023
        _insert_activity(db_path, uid, aid)
        delete_activity(db_path, aid, uid)
        delete_activity(db_path, aid, uid)  # already gone — no-op, must not raise
        assert was_deleted(db_path, uid, aid) is True
