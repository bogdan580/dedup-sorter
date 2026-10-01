"""Restore/undo endpoints and their frontend."""
from __future__ import annotations

import json
import re
from tidy.review import server as review_server
from fakes import FakeFileSystem
from tidy.duplicates.keeper import Discard, KeeperDecision
from tidy.duplicates.tiers import DuplicateGroup

from .helpers import (
    _get,
    _jpeg_bytes,
    _post_json,
)


class TestRestoreHTTP:
    """POST /api/restore + GET /api/batches.

    batch_registry is a fourth id->path allowlist, same rule as
    file_registry/series_registry/placement_registry (module docstring):
    the client only ever sends a batch_id, resolved to a real manifest path
    exclusively server-side -- see _register_batch(). Whole-batch restore
    only ("Cala paczke", user-confirmed): applier.restore_batch() runs
    exactly as --restore would, with no per-file selection.
    """

    def _selection_for(self, state, group_id):
        _, body, _ = _get(state.port, "/api/state")
        group = next(g for g in json.loads(body)["groups"] if g["group_id"] == group_id)
        return {
            "group_id": group_id,
            "keeper_id": group["keeper"]["id"],
            "discard_ids": [d["id"] for d in group["discarded"]],
        }

    def _apply(self, state, group_id):
        return _post_json(
            state.port, "/api/apply",
            headers={"X-Dedup-Token": "test-token", "Host": f"127.0.0.1:{state.port}"},
            payload={"selections": [self._selection_for(state, group_id)]},
        )

    def _restore(self, state, batch_id, extra=None):
        payload = {"batch_id": batch_id}
        if extra:
            payload.update(extra)
        return _post_json(
            state.port, "/api/restore",
            headers={"X-Dedup-Token": "test-token", "Host": f"127.0.0.1:{state.port}"},
            payload=payload,
        )

    def test_restore_with_an_unknown_batch_id_is_rejected(self, running_server_four_groups):
        httpd, state, fs = running_server_four_groups
        status, body, _ = self._restore(state, "no-such-batch")
        assert status == 400
        assert "unknown batch_id" in json.loads(body)["error"]

    def test_restore_without_a_token_is_rejected(self, running_server_four_groups):
        httpd, state, fs = running_server_four_groups
        _, apply_body, _ = self._apply(state, "1")
        batch_id = json.loads(apply_body)["batch_id"]

        status, _, _ = _post_json(
            state.port, "/api/restore",
            headers={"Host": f"127.0.0.1:{state.port}"},
            payload={"batch_id": batch_id},
        )
        assert status == 403
        # Same gate as apply -- nothing moved back.
        assert fs.exists(f"{state.archive_root}/g1/b.jpg") is False

    def test_restore_with_a_foreign_host_is_rejected(self, running_server_four_groups):
        httpd, state, fs = running_server_four_groups
        _, apply_body, _ = self._apply(state, "1")
        batch_id = json.loads(apply_body)["batch_id"]

        status, _, _ = _post_json(
            state.port, "/api/restore",
            headers={"X-Dedup-Token": "test-token", "Host": "evil.example.com"},
            payload={"batch_id": batch_id},
        )
        assert status == 403
        assert fs.exists(f"{state.archive_root}/g1/b.jpg") is False

    def test_apply_response_carries_its_own_batch_id(self, running_server_four_groups):
        httpd, state, fs = running_server_four_groups
        status, body, _ = self._apply(state, "1")
        assert status == 200
        result = json.loads(body)
        assert result["batch_id"] == "b1"
        assert result["kind"] == "dup"

    def test_restore_ignores_a_client_supplied_manifest_path(self, running_server_four_groups):
        # batch_id is the only thing handle_restore() reads out of the
        # body -- a manifest_path riding alongside it must have zero
        # effect, resolving through state.batch_registry regardless.
        httpd, state, fs = running_server_four_groups
        _, apply_body, _ = self._apply(state, "1")
        batch_id = json.loads(apply_body)["batch_id"]

        status, body, _ = self._restore(
            state, batch_id, extra={"manifest_path": "C:/not/a/real/manifest.csv"},
        )
        assert status == 200
        result = json.loads(body)
        assert result["restored"] == 1
        assert fs.exists(f"{state.archive_root}/g1/b.jpg") is True

    def test_apply_then_restore_puts_the_group_back_to_unapplied_and_the_file_back_in_place(
        self, running_server_four_groups,
    ):
        httpd, state, fs = running_server_four_groups
        discard_path = f"{state.archive_root}/g1/b.jpg"

        _, apply_body, _ = self._apply(state, "1")
        batch_id = json.loads(apply_body)["batch_id"]
        assert fs.exists(discard_path) is False

        _, state_body, _ = _get(state.port, "/api/state")
        applied_before = {g["group_id"]: g["applied"] for g in json.loads(state_body)["groups"]}
        assert applied_before["1"] is True

        status, body, _ = self._restore(state, batch_id)
        assert status == 200
        result = json.loads(body)
        assert result["restored"] == 1
        assert result["refused"] == 0
        assert fs.exists(discard_path) is True

        _, state_body2, _ = _get(state.port, "/api/state")
        applied_after = {g["group_id"]: g["applied"] for g in json.loads(state_body2)["groups"]}
        assert applied_after["1"] is False

    def test_restoring_the_same_batch_twice_refuses_the_second_time(self, running_server_four_groups):
        httpd, state, fs = running_server_four_groups
        _, apply_body, _ = self._apply(state, "1")
        batch_id = json.loads(apply_body)["batch_id"]

        first_status, _, _ = self._restore(state, batch_id)
        assert first_status == 200

        second_status, second_body, _ = self._restore(state, batch_id)
        assert second_status == 200
        second_result = json.loads(second_body)
        assert second_result["restored"] == 0
        assert second_result["refused"] == 1
        assert "already exists" in second_result["entries"][0]["detail"]

    def test_restore_after_a_rescan_does_not_unmark_a_group_with_the_same_new_number(self, tmp_path):
        # scan_generation: after a rescan every group_id is
        # renumbered from 1. Restoring a batch applied under the OLD
        # numbering must not un-mark whichever unrelated group inherited
        # the same id under the NEW one.
        archive_root = str(tmp_path / "Archive").replace("\\", "/")
        fs = FakeFileSystem()
        fs.add_dir(archive_root)
        fs.add_dir(f"{archive_root}/g1")
        fs.add_file(f"{archive_root}/g1/a.jpg", _jpeg_bytes(), volume_id=1, file_index=1)
        fs.add_file(f"{archive_root}/g1/b.jpg", _jpeg_bytes(), volume_id=1, file_index=2)
        group = DuplicateGroup(
            size=100, full_hash="hash1",
            paths=(f"{archive_root}/g1/a.jpg", f"{archive_root}/g1/b.jpg"),
        )
        decision = KeeperDecision(
            keeper=f"{archive_root}/g1/a.jpg",
            discarded=(Discard(path=f"{archive_root}/g1/b.jpg", reason="dup-exact"),),
        )

        def rescan():
            # A brand-new, unrelated "group 1" after the rescan -- exactly
            # the "unrelated group inherits the number" scenario.
            fs.add_dir(f"{archive_root}/g2")
            fs.add_file(f"{archive_root}/g2/a.jpg", _jpeg_bytes(), volume_id=1, file_index=3)
            fs.add_file(f"{archive_root}/g2/b.jpg", _jpeg_bytes(), volume_id=1, file_index=4)
            new_group = DuplicateGroup(
                size=100, full_hash="hash2",
                paths=(f"{archive_root}/g2/a.jpg", f"{archive_root}/g2/b.jpg"),
            )
            new_decision = KeeperDecision(
                keeper=f"{archive_root}/g2/a.jpg",
                discarded=(Discard(path=f"{archive_root}/g2/b.jpg", reason="dup-exact"),),
            )
            return [new_group], [new_decision], []

        httpd, thread, state = review_server.start_server(
            fs=fs, archive_root=archive_root, duplicate_groups=[group], decisions=[decision],
            thumbs_dir=str(tmp_path / "thumbs"), host="127.0.0.1", port=0,
            token="test-token", rescan=rescan,
        )
        try:
            _, apply_body, _ = self._apply(state, "1")
            batch_id = json.loads(apply_body)["batch_id"]

            rescan_status, _, _ = _post_json(
                state.port, "/api/rescan",
                headers={"X-Dedup-Token": "test-token", "Host": f"127.0.0.1:{state.port}"},
                payload={},
            )
            assert rescan_status == 200

            # Simulates "the user already applied the NEW group 1 in this
            # session" -- restoring the OLD batch must leave this alone.
            state.applied_group_ids.add("1")

            status, body, _ = self._restore(state, batch_id)
            assert status == 200
            assert json.loads(body)["restored"] == 1
            # If scan_generation weren't checked, this would have been
            # wrongly cleared by the stale batch's restore.
            assert "1" in state.applied_group_ids
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_get_batches_lists_a_batch_created_by_apply(self, running_server_four_groups):
        httpd, state, fs = running_server_four_groups
        self._apply(state, "1")

        status, body, _ = _get(state.port, "/api/batches")
        assert status == 200
        batches_out = json.loads(body)["batches"]
        assert len(batches_out) == 1
        assert batches_out[0]["kind"] == "remove"
        assert batches_out[0]["restorable"] == 1
        assert batches_out[0]["already_restored"] is False
        assert batches_out[0]["error"] == ""

    def test_get_batches_reflects_a_restore_and_shares_the_apply_batch_id(
        self, running_server_four_groups,
    ):
        httpd, state, fs = running_server_four_groups
        _, apply_body, _ = self._apply(state, "1")
        batch_id = json.loads(apply_body)["batch_id"]
        self._restore(state, batch_id)

        status, body, _ = _get(state.port, "/api/batches")
        batches_out = json.loads(body)["batches"]
        assert batches_out[0]["restorable"] == 0
        assert batches_out[0]["already_restored"] is True
        # Same id in both places -- the property every registry in this
        # module guarantees (module docstring's table, extended to batches).
        assert batches_out[0]["batch_id"] == batch_id

    def test_get_batches_needs_no_token_like_get_state(self, running_server_four_groups):
        httpd, state, fs = running_server_four_groups
        self._apply(state, "1")
        # No X-Dedup-Token -- GET /api/batches is read-only (module
        # docstring: "jak /api/state"), so it must still succeed.
        status, _, _ = _get(state.port, "/api/batches")
        assert status == 200

    def test_batch_entries_unknown_batch_id_rejected_like_restore(
        self, running_server_four_groups,
    ):
        httpd, state, fs = running_server_four_groups
        status, body, _ = _get(state.port, "/api/batch-entries?batch_id=no-such-batch")
        assert status == 400
        assert "unknown batch_id" in json.loads(body)["error"]

    def test_batch_entries_returns_thumb_id_for_a_supported_file(
        self, running_server_four_groups,
    ):
        httpd, state, fs = running_server_four_groups
        _, apply_body, _ = self._apply(state, "1")
        batch_id = json.loads(apply_body)["batch_id"]

        status, body, _ = _get(state.port, "/api/batch-entries?batch_id=" + batch_id)
        assert status == 200
        entries = json.loads(body)["entries"]
        assert len(entries) == 1
        assert entries[0]["restorable"] is True
        assert entries[0]["thumb_id"] is not None
        assert entries[0]["thumb_kind"] == "image"

    def test_batch_entries_thumb_id_resolves_via_get_thumb(self, running_server_four_groups):
        # thumb_id must be registered in state.thumb_sources -- the same
        # allowlist _serve_thumb() resolves every GET /api/thumb/<id>
        # against (module docstring's registry table, extended to
        # thumb_sources-only for this screen).
        httpd, state, fs = running_server_four_groups
        _, apply_body, _ = self._apply(state, "1")
        batch_id = json.loads(apply_body)["batch_id"]

        _, body, _ = _get(state.port, "/api/batch-entries?batch_id=" + batch_id)
        thumb_id = json.loads(body)["entries"][0]["thumb_id"]

        status, _, headers = _get(state.port, "/api/thumb/" + thumb_id)
        assert status == 200
        assert headers.get("Content-Type") == "image/jpeg"

    def test_batch_entries_no_thumb_id_once_the_file_is_already_restored(
        self, running_server_four_groups,
    ):
        # Once restored, the batch-dir copy resolve_dst() points at is gone
        # -- restorable flips to False, and thumb_id must follow it to None
        # even though the extension still "supports()" a thumbnail: this
        # function never renders a broken image for a file it can no
        # longer vouch for (build_batch_entries_view()'s own docstring).
        httpd, state, fs = running_server_four_groups
        _, apply_body, _ = self._apply(state, "1")
        batch_id = json.loads(apply_body)["batch_id"]
        self._restore(state, batch_id)

        status, body, _ = _get(state.port, "/api/batch-entries?batch_id=" + batch_id)
        assert status == 200
        entries = json.loads(body)["entries"]
        assert len(entries) == 1
        assert entries[0]["restorable"] is False
        assert entries[0]["thumb_id"] is None

    def test_batch_entries_needs_no_token_like_get_batches(self, running_server_four_groups):
        httpd, state, fs = running_server_four_groups
        _, apply_body, _ = self._apply(state, "1")
        batch_id = json.loads(apply_body)["batch_id"]
        # No X-Dedup-Token -- read-only, same as GET /api/batches itself.
        status, _, _ = _get(state.port, "/api/batch-entries?batch_id=" + batch_id)
        assert status == 200


class TestRestoreFrontend:
    """Source-level assertions on the served page (no in-repo JS execution
    harness): the "Cofnij" tab exists and submitRestore() only ever sends a
    batch_id, never a path -- mirrors TestThreeTabsFrontendRunda2Krok4's own
    approach above."""

    def _html(self, running_server):
        httpd, state = running_server
        status, body, _ = _get(state.port, "/")
        assert status == 200
        return body.decode("utf-8")

    def _function_body(self, html, name):
        match = re.search(
            r'function\s+%s\s*\([^)]*\)\s*\{(.*?)\n\}' % re.escape(name),
            html, re.DOTALL,
        )
        assert match, f"expected a function named {name}()"
        return match.group(1)

    def test_cofnij_tab_button_and_screen_container_exist(self, running_server):
        # Label renamed to "Restore"; the
        # internal tab key/id (data-tab="restore", #restore-screen) is
        # unaffected -- this is a label change, not a refactor.
        html = self._html(running_server)
        assert 'data-tab="restore"' in html
        assert ">Restore<" in html
        assert 'id="restore-screen"' in html

    def test_submit_restore_posts_batch_id_only_never_a_path(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "submitRestore")
        assert '"/api/restore"' in body
        assert "batch_id: batchId" in body
        assert "manifest_path" not in body
        assert "batch_dir" not in body

    def test_restore_screen_buttons_call_submit_restore_with_the_servers_batch_id(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "renderRestoreScreen")
        assert "submitRestore(b.batch_id" in body

    def test_load_batches_fetches_the_batches_endpoint(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "loadBatches")
        assert 'fetch("/api/batches")' in body

    def test_load_batches_never_fetches_a_single_batchs_entries_up_front(self, running_server):
        # The whole point of lazy, per-batch expansion ("chce
        # miec podglad miniaturek", but a batch can hold up to 135 files):
        # loadBatches() runs on every "Cofnij" tab open and must never
        # itself request one batch's file list.
        html = self._html(running_server)
        body = self._function_body(html, "loadBatches")
        assert "batch-entries" not in body

    def test_restore_screen_offers_a_per_batch_show_files_toggle(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "renderRestoreScreen")
        assert "toggleBatchEntries(b.batch_id)" in body
        assert "js_show_files" in body

    def test_toggle_batch_entries_fetches_the_batch_entries_endpoint(self, running_server):
        html = self._html(running_server)
        body = self._function_body(html, "toggleBatchEntries")
        assert '"/api/batch-entries?batch_id="' in body

    def test_batch_entries_row_reuses_the_shared_card_renderer(self, running_server):
        # "Reuse the existing thumbnail rendering/CSS" -- makeCard()/
        # computeGroupPathHighlights() are the same renderer the dup/
        # series/events/placement screens already share, not a parallel one.
        html = self._html(running_server)
        body = self._function_body(html, "renderBatchEntriesRow")
        assert "makeCard(" in body
        assert "computeGroupPathHighlights(" in body
        assert "readOnly: true" in body
