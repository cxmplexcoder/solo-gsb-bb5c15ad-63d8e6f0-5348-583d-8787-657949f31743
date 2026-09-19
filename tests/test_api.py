"""End-to-end API tests against a temporary SQLite database."""

import os
import tempfile

os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    # Context-manager form runs the lifespan, which creates the tables.
    with TestClient(app) as c:
        yield c


BASE = {"replicas": 1, "image": "app:v1", "db": {"host": "a", "port": 5432}}
LEFT = {"replicas": 3, "image": "app:v1", "db": {"host": "b", "port": 5432}}
RIGHT = {"replicas": 1, "image": "app:v2", "db": {"host": "c", "port": 5433}}


def create_merge(client, key=None):
    payload = {"base": BASE, "left": LEFT, "right": RIGHT}
    if key:
        payload["idempotency_key"] = key
    resp = client.post("/merges", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_auto_merge_and_conflicts(client):
    merge = create_merge(client)
    assert merge["status"] == "pending"
    assert merge["revision"] == 1
    # Single-side changes adopted, conflict only on /db/host.
    assert merge["document"]["replicas"] == 3
    assert merge["document"]["image"] == "app:v2"
    assert merge["document"]["db"]["port"] == 5433
    pointers = [c["pointer"] for c in merge["conflicts"]]
    assert pointers == ["/db/host"]
    conflict = merge["conflicts"][0]
    assert conflict["left"] == "b" and conflict["right"] == "c"
    assert merge["document"]["db"]["host"] == {"__merge_conflict__": "/db/host"}


def test_batch_resolution_to_immutable_result(client):
    merge = create_merge(client)
    mid = merge["merge_id"]

    # Batch 1: wrong pointer -> 400, nothing applied.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 1, "resolutions": [{"pointer": "/nope", "value": 1}]},
    )
    assert resp.status_code == 400

    # Batch 2: resolve the conflict with an explicit value.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={
            "revision": 1,
            "resolutions": [{"pointer": "/db/host", "value": "final-host"}],
        },
    )
    assert resp.status_code == 200, resp.text
    done = resp.json()
    assert done["status"] == "resolved"
    assert done["revision"] == 2
    assert done["conflicts"] == []
    assert done["result"]["db"]["host"] == "final-host"
    assert done["result"]["replicas"] == 3

    # Result is immutable: further resolutions are rejected with 409.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 2, "resolutions": [{"pointer": "/db/host", "value": "x"}]},
    )
    assert resp.status_code == 409

    # Status query returns the same immutable result.
    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["result"] == done["result"]


def test_stale_revision_returns_409(client):
    merge = create_merge(client)
    mid = merge["merge_id"]
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 999, "resolutions": [{"pointer": "/db/host", "choice": "left"}]},
    )
    assert resp.status_code == 409
    assert resp.json()["current_revision"] == 1

    # Choice-based resolution still works with the right revision.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 1, "resolutions": [{"pointer": "/db/host", "choice": "right"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["result"]["db"]["host"] == "c"


def test_partial_batches_bump_revision(client):
    merge = create_merge(client)
    # Add a second conflict so batches can be split.
    resp = client.post(
        "/merges",
        json={
            "base": {"a": 1, "b": 1},
            "left": {"a": 2, "b": 2},
            "right": {"a": 3, "b": 3},
        },
    )
    mid = resp.json()["merge_id"]
    assert resp.json()["conflict_count"] == 2

    # First batch resolves /a only; revision bumps to 2, still pending.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 1, "resolutions": [{"pointer": "/a", "choice": "left"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["revision"] == 2
    assert [c["pointer"] for c in body["conflicts"]] == ["/b"]

    # Old revision is now stale.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 1, "resolutions": [{"pointer": "/b", "value": 9}]},
    )
    assert resp.status_code == 409

    # Second batch finishes the merge.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 2, "resolutions": [{"pointer": "/b", "value": 9}]},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"
    assert resp.json()["result"] == {"a": 2, "b": 9}


def test_resolution_preview_is_side_effect_free(client):
    merge = create_merge(client)
    mid = merge["merge_id"]

    resp = client.post(
        f"/merges/{mid}/resolution-preview",
        json={"revision": 1, "resolutions": [{"pointer": "/db/host", "choice": "left"}]},
    )
    assert resp.status_code == 200, resp.text
    preview = resp.json()
    assert preview["merge_id"] == mid
    assert preview["revision"] == 1
    assert preview["document"]["db"]["host"] == "b"
    assert preview["document"]["replicas"] == 3
    assert preview["remaining_conflicts"] == []
    assert preview["would_resolve"] is True

    # A fully resolving preview carries the deterministic patch and digest.
    from app.patch import sha256_hex

    assert preview["result_sha256"] == sha256_hex(preview["document"])
    from tests.patch_apply import apply_patch

    assert apply_patch(BASE, preview["patch"]) == preview["document"]

    # Nothing persisted: revision, status and conflicts are untouched.
    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["revision"] == 1
    assert fetched["status"] == "pending"
    assert [c["pointer"] for c in fetched["conflicts"]] == ["/db/host"]

    # The same batch is still accepted for real against revision 1.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 1, "resolutions": [{"pointer": "/db/host", "choice": "left"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["result"] == preview["document"]


def test_resolution_preview_partial_batch(client):
    resp = client.post(
        "/merges",
        json={
            "base": {"a": 1, "b": 1},
            "left": {"a": 2, "b": 2},
            "right": {"a": 3, "b": 3},
        },
    )
    mid = resp.json()["merge_id"]

    resp = client.post(
        f"/merges/{mid}/resolution-preview",
        json={"revision": 1, "resolutions": [{"pointer": "/a", "value": 7}]},
    )
    assert resp.status_code == 200
    preview = resp.json()
    assert preview["would_resolve"] is False
    assert preview["document"] == {"a": 7, "b": {"__merge_conflict__": "/b"}}
    assert [c["pointer"] for c in preview["remaining_conflicts"]] == ["/b"]
    # No patch/digest until the batch would resolve everything.
    assert "patch" not in preview
    assert "result_sha256" not in preview


def test_resolution_preview_rejects_invalid_batch(client):
    merge = create_merge(client)
    mid = merge["merge_id"]

    # Unknown pointer.
    resp = client.post(
        f"/merges/{mid}/resolution-preview",
        json={"revision": 1, "resolutions": [{"pointer": "/nope", "value": 1}]},
    )
    assert resp.status_code == 400

    # Duplicate pointer within one batch.
    resp = client.post(
        f"/merges/{mid}/resolution-preview",
        json={
            "revision": 1,
            "resolutions": [
                {"pointer": "/db/host", "choice": "left"},
                {"pointer": "/db/host", "choice": "right"},
            ],
        },
    )
    assert resp.status_code == 400

    # Both choice and value on the same item.
    resp = client.post(
        f"/merges/{mid}/resolution-preview",
        json={
            "revision": 1,
            "resolutions": [{"pointer": "/db/host", "choice": "left", "value": "x"}],
        },
    )
    assert resp.status_code == 400

    # An explicit null value still counts as giving 'value'.
    resp = client.post(
        f"/merges/{mid}/resolution-preview",
        json={
            "revision": 1,
            "resolutions": [{"pointer": "/db/host", "choice": "left", "value": None}],
        },
    )
    assert resp.status_code == 400

    # Failed previews leave the merge untouched as well.
    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["revision"] == 1
    assert fetched["status"] == "pending"


def test_resolution_preview_revision_and_state_guards(client):
    merge = create_merge(client)
    mid = merge["merge_id"]

    # Stale revision -> 409 with the current revision echoed.
    resp = client.post(
        f"/merges/{mid}/resolution-preview",
        json={"revision": 999, "resolutions": [{"pointer": "/db/host", "choice": "left"}]},
    )
    assert resp.status_code == 409
    assert resp.json()["current_revision"] == 1

    # Unknown merge -> 404.
    resp = client.post(
        "/merges/does-not-exist/resolution-preview",
        json={"revision": 1, "resolutions": [{"pointer": "/db/host", "choice": "left"}]},
    )
    assert resp.status_code == 404

    # Resolve for real; a preview against the finished merge -> 409.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={"revision": 1, "resolutions": [{"pointer": "/db/host", "choice": "right"}]},
    )
    assert resp.status_code == 200
    resp = client.post(
        f"/merges/{mid}/resolution-preview",
        json={"revision": 2, "resolutions": [{"pointer": "/db/host", "choice": "left"}]},
    )
    assert resp.status_code == 409


def test_idempotency_key_replays_merge(client):
    first = create_merge(client, key="order-123")
    resp = client.post(
        "/merges",
        json={"base": BASE, "left": LEFT, "right": RIGHT, "idempotency_key": "order-123"},
    )
    assert resp.status_code == 200  # replay, not 201
    assert resp.json()["merge_id"] == first["merge_id"]

    # Header form works too.
    resp = client.post(
        "/merges",
        json={"base": BASE, "left": LEFT, "right": RIGHT},
        headers={"Idempotency-Key": "order-123"},
    )
    assert resp.json()["merge_id"] == first["merge_id"]


def test_no_conflict_merge_is_immediately_resolved(client):
    resp = client.post(
        "/merges",
        json={"base": {"a": 1}, "left": {"a": 2}, "right": {"a": 2}},
    )
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["result"] == {"a": 2}


def test_unknown_merge_404(client):
    assert client.get("/merges/does-not-exist").status_code == 404
    assert client.get("/merges/does-not-exist/patch").status_code == 404


def test_patch_pending_returns_409(client):
    merge = create_merge(client)
    assert merge["status"] == "pending"
    resp = client.get(f"/merges/{merge['merge_id']}/patch")
    assert resp.status_code == 409
    assert "detail" in resp.json()


def test_patch_for_resolved_merge(client):
    merge = create_merge(client)
    mid = merge["merge_id"]
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={
            "revision": 1,
            "resolutions": [{"pointer": "/db/host", "value": "final-host"}],
        },
    )
    assert resp.status_code == 200
    result = resp.json()["result"]

    resp = client.get(f"/merges/{mid}/patch")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["merge_id"] == mid
    assert body["status"] == "resolved"
    assert body["patch"] == [
        {"op": "replace", "path": "/db/host", "value": "final-host"},
        {"op": "replace", "path": "/db/port", "value": 5433},
        {"op": "replace", "path": "/image", "value": "app:v2"},
        {"op": "replace", "path": "/replicas", "value": 3},
    ]

    # Digests are stable SHA-256 of canonical base / result.
    from app.patch import sha256_hex

    assert body["base_sha256"] == sha256_hex(BASE)
    assert body["result_sha256"] == sha256_hex(result)
    assert len(body["base_sha256"]) == 64

    # Applying ops in the returned order to the base rebuilds the result.
    from tests.patch_apply import apply_patch

    assert apply_patch(BASE, body["patch"]) == result


def test_patch_for_immediately_resolved_merge(client):
    resp = client.post(
        "/merges",
        json={"base": {"a": 1, "b": 2}, "left": {"a": 9}, "right": {"a": 9}},
    )
    mid = resp.json()["merge_id"]
    body = client.get(f"/merges/{mid}/patch").json()
    assert body["patch"] == [
        {"op": "replace", "path": "/a", "value": 9},
        {"op": "remove", "path": "/b"},
    ]


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def _multi_conflict_merge(client):
    """Merge with conflicts at /db/host, /db/tls/cert and /cache/size."""
    resp = client.post(
        "/merges",
        json={
            "base": {
                "db": {"host": "a", "tls": {"cert": "base.pem"}},
                "cache": {"size": 10},
            },
            "left": {
                "db": {"host": "b", "tls": {"cert": "left.pem"}},
                "cache": {"size": 20},
            },
            "right": {
                "db": {"host": "c", "tls": {"cert": "right.pem"}},
                "cache": {"size": 30},
            },
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_rule_resolutions_match_and_commit_once(client):
    merge = _multi_conflict_merge(client)
    mid = merge["merge_id"]
    pointers = sorted(c["pointer"] for c in merge["conflicts"])
    assert pointers == ["/cache/size", "/db/host", "/db/tls/cert"]

    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={
            "revision": 1,
            "rules": [
                {"pattern": "/db/host", "choice": "left"},
                {"pattern": "/db/**", "choice": "right"},
                {"pattern": "/*/size", "choice": "left"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "resolved"
    # Exactly one revision bump for the whole rule batch.
    assert body["revision"] == 2
    assert body["conflicts"] == []
    assert body["unmatched_conflicts"] == []

    # Each rule reports the pointers it won (order irrelevant).
    by_index = {r["rule_index"]: r for r in body["matched_rules"]}
    assert by_index[0]["pattern"] == "/db/host"
    assert by_index[0]["pointers"] == ["/db/host"]
    assert by_index[1]["pattern"] == "/db/**"
    assert by_index[1]["pointers"] == ["/db/tls/cert"]
    assert by_index[2]["pattern"] == "/*/size"
    assert by_index[2]["pointers"] == ["/cache/size"]

    # Choices landed in the immutable result.
    result = body["result"]
    assert result["db"]["host"] == "b"
    assert result["db"]["tls"]["cert"] == "right.pem"
    assert result["cache"]["size"] == 20

    # Result is immutable afterwards.
    again = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 2, "rules": [{"pattern": "/**", "choice": "left"}]},
    )
    assert again.status_code == 409


def test_rule_specificity_order(client):
    """More literals win, then fewer '*', then declaration order."""
    merge = _multi_conflict_merge(client)
    mid = merge["merge_id"]

    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={
            "revision": 1,
            "rules": [
                {"pattern": "/**", "choice": "right"},
                {"pattern": "/db/**", "choice": "left"},
                {"pattern": "/db/*", "choice": "right"},
                {"pattern": "/db/host", "choice": "left"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    winners = {r["rule_index"]: sorted(r["pointers"]) for r in body["matched_rules"]}
    # Exact literal beats all wildcards -> /db/host via rule 3.
    assert winners[3] == ["/db/host"]
    # /db/* has only two segments and cannot match /db/tls/cert; that
    # conflict goes to /db/** (more literals than the bare /**).
    assert winners[2] == []
    assert winners[1] == ["/db/tls/cert"]
    assert winners[0] == ["/cache/size"]

    result = body["result"]
    assert result["db"]["host"] == "b"  # rule 3 left
    assert result["db"]["tls"]["cert"] == "left.pem"  # rule 1 left
    assert result["cache"]["size"] == 30  # rule 0 right


def test_rule_identical_patterns_use_declaration_order(client):
    merge = _multi_conflict_merge(client)
    mid = merge["merge_id"]
    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={
            "revision": 1,
            "rules": [
                {"pattern": "/db/*", "choice": "left"},
                {"pattern": "/db/*", "choice": "right"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Only /db/host is directly under /db; /db/tls/cert is two levels
    # down and stays unmatched. The earlier rule wins the one hit.
    winners = {r["rule_index"]: r["pointers"] for r in body["matched_rules"]}
    assert winners[0] == ["/db/host"]
    assert winners[1] == []
    assert sorted(c["pointer"] for c in body["unmatched_conflicts"]) == [
        "/cache/size",
        "/db/tls/cert",
    ]
    assert body["status"] == "pending"
    assert body["revision"] == 2
    assert body["result"] is None


def test_rule_unmatched_conflicts_remain_open(client):
    merge = _multi_conflict_merge(client)
    mid = merge["merge_id"]
    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={
            "revision": 1,
            "rules": [{"pattern": "/db/host", "choice": "right"}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    # One revision bump for the batch; two conflicts left.
    assert body["revision"] == 2
    assert sorted(c["pointer"] for c in body["conflicts"]) == [
        "/cache/size",
        "/db/tls/cert",
    ]
    assert sorted(c["pointer"] for c in body["unmatched_conflicts"]) == [
        "/cache/size",
        "/db/tls/cert",
    ]
    assert body["document"]["db"]["host"] == "c"
    assert body["document"]["db"]["tls"]["cert"] == {
        "__merge_conflict__": "/db/tls/cert"
    }

    # The remainder can be finished with the ordinary pointer endpoint.
    resp = client.post(
        f"/merges/{mid}/resolutions",
        json={
            "revision": 2,
            "resolutions": [
                {"pointer": "/cache/size", "value": 99},
                {"pointer": "/db/tls/cert", "choice": "left"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "resolved"
    assert resp.json()["revision"] == 3


def test_rule_no_match_still_bumps_revision_once(client):
    merge = _multi_conflict_merge(client)
    mid = merge["merge_id"]
    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={
            "revision": 1,
            "rules": [{"pattern": "/nonexistent/*", "choice": "left"}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["revision"] == 2
    assert body["matched_rules"][0]["pointers"] == []
    assert len(body["unmatched_conflicts"]) == 3
    # No document change: placeholders are still in place.
    assert body["document"]["db"]["host"] == {"__merge_conflict__": "/db/host"}

    # The old revision is now stale.
    stale = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 1, "rules": [{"pattern": "/**", "choice": "left"}]},
    )
    assert stale.status_code == 409
    assert stale.json()["current_revision"] == 2


def test_rule_stale_revision_returns_409(client):
    merge = _multi_conflict_merge(client)
    mid = merge["merge_id"]
    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={
            "revision": 999,
            "rules": [{"pattern": "/db/host", "choice": "left"}],
        },
    )
    assert resp.status_code == 409
    assert resp.json()["current_revision"] == 1
    # Nothing applied.
    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["revision"] == 1
    assert fetched["status"] == "pending"


def test_rule_unknown_merge_404(client):
    resp = client.post(
        "/merges/does-not-exist/rule-resolutions",
        json={"revision": 1, "rules": [{"pattern": "/**", "choice": "left"}]},
    )
    assert resp.status_code == 404


def test_rule_missing_side_honours_deletion(client):
    # left deletes /a, right modifies it -> conflict with missing side.
    resp = client.post(
        "/merges",
        json={
            "base": {"a": 1, "b": 1},
            "left": {"b": 1},
            "right": {"a": 9, "b": 2},
        },
    )
    mid = resp.json()["merge_id"]
    assert {c["pointer"]: c["missing"] for c in resp.json()["conflicts"]} == {
        "/a": ["left"]
    }
    # Choosing the deleting side removes the key...
    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 1, "rules": [{"pattern": "/*", "choice": "left"}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # /* matches only one segment: /a is removed (deletion), /b stays
    # untouched because it was never in conflict.
    assert body["status"] == "resolved"
    assert body["result"] == {"b": 2}


def test_rule_invalid_pattern_400_without_changes(client):
    merge = _multi_conflict_merge(client)
    mid = merge["merge_id"]

    for bad_pattern in [
        "db/host",        # missing leading slash
        "/db/host-*",     # '*' not a whole segment
        "/db/**/host",    # '**' not the final segment
        "/db/ho~st",      # bad '~' escape
    ]:
        resp = client.post(
            f"/merges/{mid}/rule-resolutions",
            json={"revision": 1, "rules": [{"pattern": bad_pattern, "choice": "left"}]},
        )
        assert resp.status_code == 400, (bad_pattern, resp.text)

    # Even one bad rule among many aborts everything.
    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={
            "revision": 1,
            "rules": [
                {"pattern": "/db/host", "choice": "left"},
                {"pattern": "**", "choice": "right"},
            ],
        },
    )
    assert resp.status_code == 400

    # Empty rule list is a schema error (422), not a silent success.
    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 1, "rules": []},
    )
    assert resp.status_code == 422

    fetched = client.get(f"/merges/{mid}").json()
    assert fetched["revision"] == 1
    assert fetched["status"] == "pending"
    assert len(fetched["conflicts"]) == 3


def test_rule_one_unapplicable_choice_aborts_all(client):
    """All hits validate first; failure leaves zero partial changes.

    A root conflict resolved by choosing a missing side would require
    deleting the whole document, which is rejected. The API cannot create
    such a state through normal input, so seed the row directly.
    """
    from app import db
    from app.store import STATUS_PENDING

    merge_id = "seeded-root-conflict"
    now = "2026-01-01T00:00:00+00:00"
    conflict = {
        "pointer": "",
        "base": {"keep": 1},
        "left": None,
        "right": {"keep": 2},
        "missing": ["left"],
    }
    db.execute(
        """
        INSERT INTO merges (
            id, idempotency_key, status, revision,
            base_doc, left_doc, right_doc,
            document, conflicts, result, created_at, updated_at
        ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
        """,
        (
            merge_id,
            STATUS_PENDING,
            1,
            json_dumps({"keep": 1}),
            json_dumps({"keep": 1}),
            json_dumps({"keep": 2}),
            json_dumps({"__merge_conflict__": ""}),
            json_dumps([conflict]),
            now,
            now,
        ),
    )

    # Two rules hit: root via ** and nothing else. Choosing the missing
    # left side must fail the whole batch as 400.
    resp = client.post(
        f"/merges/{merge_id}/rule-resolutions",
        json={
            "revision": 1,
            "rules": [
                {"pattern": "/something", "choice": "right"},
                {"pattern": "/**", "choice": "left"},
            ],
        },
    )
    assert resp.status_code == 400, resp.text

    fetched = client.get(f"/merges/{merge_id}").json()
    assert fetched["revision"] == 1
    assert fetched["status"] == "pending"
    assert fetched["conflicts"] == [conflict]
    assert fetched["document"] == {"__merge_conflict__": ""}

    # The same conflict is resolvable by choosing the present side.
    resp = client.post(
        f"/merges/{merge_id}/rule-resolutions",
        json={"revision": 1, "rules": [{"pattern": "/**", "choice": "right"}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == {"keep": 2}


def json_dumps(value):
    import json

    return json.dumps(value)


def test_rule_double_star_zero_segment_match(client):
    # A conflict directly at /db is matched by /db/** (zero tail segments).
    resp = client.post(
        "/merges",
        json={
            "base": {"db": {"host": "a"}},
            "left": {"db": {"host": "b"}},
            "right": {"db": {"host": "c"}},
        },
    )
    mid = resp.json()["merge_id"]
    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={"revision": 1, "rules": [{"pattern": "/db/**", "choice": "right"}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["matched_rules"][0]["pointers"] == ["/db/host"]
    assert resp.json()["result"]["db"]["host"] == "c"


def test_rule_escaped_literal_pattern(client):
    resp = client.post(
        "/merges",
        json={
            "base": {"a/b": 1},
            "left": {"a/b": 2},
            "right": {"a/b": 3},
        },
    )
    mid = resp.json()["merge_id"]
    assert resp.json()["conflicts"][0]["pointer"] == "/a~1b"
    resp = client.post(
        f"/merges/{mid}/rule-resolutions",
        json={
            "revision": 1,
            "rules": [
                {"pattern": "/*", "choice": "right"},
                {"pattern": "/a~1b", "choice": "left"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    # Literal pattern beats the single-star wildcard.
    assert resp.json()["result"] == {"a/b": 2}
