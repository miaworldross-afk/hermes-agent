"""Two lifecycle invariants, using real SQLite and a local GitHub HTTP contract."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from hermes_cli.kanban_pr_acceptance import collect_acceptance


@pytest.fixture
def github(tmp_path, monkeypatch):
    state = {"conclusion": "success", "head": "a" * 40, "reads": 0, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            sha = state["head"]
            if self.path == "/graphql":
                protection = None if state.get("plan_gated") else {"requiredStatusChecks": [
                    {"context": "required", "app": {"databaseId": 1}}]}
                value = {"data": {"repository": {"pullRequest": {
                    "headRefOid": sha, "baseRefName": "main", "state": "OPEN",
                    "baseRef": {"branchProtectionRule": protection}}}}}
            elif "/rules/branches/" in self.path:
                if state.get("rules_error"):
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(json.dumps({"message": state["rules_error"]}).encode())
                    return
                value = [[]]
            elif "/check-runs" in self.path:
                run = {"id": 42, "name": state.get("context", "required"), "head_sha": sha,
                       "app": {"id": state.get("app_id", 1)}, "status": "in_progress" if state["conclusion"] == "pending" else "completed", "conclusion": state["conclusion"],
                       "html_url": "https://github.com/acme/repo/actions/runs/42"}
                if state.get("stale"):
                    run["head_sha"] = "b" * 40
                runs = [] if state.get("missing") else [run]
                value = [{"total_count": 100 + len(runs), "check_runs": [
                    {**run, "id": 1000 + i, "name": "optional", "conclusion": "skipped"}
                    for i in range(100)]}, {"total_count": 100 + len(runs), "check_runs": runs}]
                if state.get("race"):
                    state["race"]()
                if state.get("head_change"):
                    state["head"] = "b" * 40
            elif "/statuses" in self.path:
                value = [[]]
            elif "/pulls/" in self.path:
                value = {"head": {"sha": sha}, "base": {"ref": "main"}, "state": "open"}
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport json,os,sys,urllib.error,urllib.request\n"
                  "real=os.environ.get('HERMES_REAL_HOME')\n"
                  "assert not real or os.environ.get('HOME') == real\n"
                  f"u='http://127.0.0.1:{server.server_port}/'+sys.argv[2]\n"
                  "try:\n"
                  " print(urllib.request.urlopen(u).read().decode())\n"
                  "except urllib.error.HTTPError as e:\n"
                  " body=json.loads(e.read().decode())\n"
                  " print(f\"gh: {body['message']} (HTTP {e.code})\",file=sys.stderr)\n"
                  " raise SystemExit(1)\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HOME", str(tmp_path / "profile-home"))
    monkeypatch.setenv("HERMES_REAL_HOME", str(tmp_path / "real-home"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.linux_only
def test_pr_completion_requires_current_required_evidence(github):
    with connect() as conn:
        for conclusion in ("failure", "pending", "cancelled", "timed_out", "action_required", "neutral", "skipped", None, "success"):
            github.update(conclusion=conclusion, head="a" * 40)
            tid = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
            ok = kb.complete_task(conn, tid, metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert ok is (conclusion == "success")
            task = kb.get_task(conn, tid)
            assert (task.status == "done") is ok
            receipts = [json.loads(r[0]) for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
            assert receipts and receipts[-1]["head_sha"] == "a" * 40
            if not ok:
                assert task.status in {"running", "ready", "blocked", "review"}
                assert "retry" in receipts[-1]["recovery"]
                assert receipts[-1]["checks"][0]["id"] == 42
        for fault in ("missing", "stale", "head_change"):
            github.update(conclusion="success", head="a" * 40)
            github[fault] = True
            tid = kb.create_task(conn, title=fault, completion_contract="acme/repo")
            assert not kb.complete_task(conn, tid, metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).status != "done"
            github.pop(fault)
        # Omission and a sibling repository cannot downgrade the stored declaration.
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        assert not kb.complete_task(conn, tid, summary="local green")
        assert not kb.complete_task(conn, tid, metadata={"published_pr": "https://github.com/other/repo/pull/7"})
        before = len(github["requests"])
        local = kb.create_task(conn, title="local", completion_contract="local-only")
        assert kb.complete_task(conn, local, summary="https://github.com/acme/repo/pull/7 is background context")
        assert len(github["requests"]) == before


@pytest.mark.linux_only
def test_acceptance_receipts_and_terminal_write_share_run_ownership(github):
    with connect() as conn:
        for conclusion in ("success", "failure"):
            tid = kb.create_task(conn, title="race", completion_contract="acme/repo")
            owner = kb.claim_task(conn, tid)
            run_id = owner.current_run_id
            def reclaim():
                with connect() as rival:
                    assert kb.block_task(rival, tid, reason="Reassigned during acceptance")
                    assert kb.unblock_task(rival, tid)
                    github["replacement"] = kb.claim_task(rival, tid).current_run_id
            github.update(conclusion=conclusion, race=reclaim)
            assert not kb.complete_task(conn, tid, expected_run_id=run_id,
                metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).current_run_id == github["replacement"]
            assert github["replacement"] != run_id
            assert kb.get_task(conn, tid).status != "done"
            assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0] == 0
            github.pop("race")


@pytest.mark.linux_only
def test_plan_gated_rules_use_only_exact_repository_policy(github, monkeypatch):
    """A real rules 403 may use a preconfigured repo/app/context policy, nothing broader."""
    from hermes_cli import config as config_mod

    policy = {"kanban": {"pr_acceptance": {"repository_policies": {
        "acme/repo": {"required_status_checks": [
            {"context": "required", "app_id": 1},
        ]},
    }}}}
    monkeypatch.setattr(config_mod, "load_config_readonly", lambda: policy)
    github.update(
        plan_gated=True,
        rules_error="Upgrade to GitHub Pro or make this repository public to enable this feature.",
        conclusion="success",
        head="a" * 40,
    )

    accepted = collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")

    assert accepted["ok"] is True
    assert accepted["classification"] == "success"
    assert accepted["required"] == [{"context": "required", "app_id": 1}]
    assert accepted["policy_source"] == "configured_repository_fallback"
    assert accepted["head_sha"] == "a" * 40
    assert any("/pulls/7" in request for request in github["requests"])
    with connect() as conn:
        tid = kb.create_task(conn, title="plan-gated", completion_contract="acme/repo")
        assert kb.complete_task(
            conn,
            tid,
            metadata={"published_pr": "https://github.com/acme/repo/pull/7"},
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "done"


@pytest.mark.linux_only
@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("no_policy", "missing"),
        ("wrong_context", "missing"),
        ("wrong_app", "missing"),
        ("missing", "missing"),
        ("pending", "pending"),
        ("failure", "failure"),
        ("cancelled", "infra"),
        ("stale", "stale"),
        ("head_change", "stale"),
    ],
)
def test_plan_gated_fallback_rejects_every_non_exact_success(github, monkeypatch, mutation, expected):
    from hermes_cli import config as config_mod

    repositories = {"acme/repo": {"required_status_checks": [
        {"context": "required", "app_id": 1},
    ]}}
    monkeypatch.setattr(config_mod, "load_config_readonly", lambda: {
        "kanban": {"pr_acceptance": {"repository_policies": repositories}}
    })
    github.update(
        plan_gated=True,
        rules_error="Upgrade to GitHub Pro or make this repository public to enable this feature.",
        conclusion="success",
        head="a" * 40,
        context="required",
        app_id=1,
    )
    if mutation == "no_policy":
        repositories.clear()
    elif mutation == "wrong_context":
        github["context"] = "similar-but-not-required"
    elif mutation == "wrong_app":
        github["app_id"] = 999
    elif mutation in {"missing", "stale", "head_change"}:
        github[mutation] = True
    else:
        github["conclusion"] = mutation

    receipt = collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")

    assert receipt["ok"] is False
    assert receipt["classification"] == expected


@pytest.mark.linux_only
@pytest.mark.parametrize(
    "message",
    [
        "Resource not accessible by personal access token",
        "Upgrade to GitHub Pro or make this repository public to enable this feature",  # not exact
    ],
)
def test_fallback_rejects_ordinary_or_unrecognized_rules_403(github, monkeypatch, message):
    from hermes_cli import config as config_mod

    monkeypatch.setattr(config_mod, "load_config_readonly", lambda: {
        "kanban": {"pr_acceptance": {"repository_policies": {
            "acme/repo": {"required_status_checks": [{"context": "required", "app_id": 1}]},
        }}}
    })
    github.update(plan_gated=True, rules_error=message, conclusion="success", head="a" * 40)

    receipt = collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")

    assert receipt["ok"] is False
    assert receipt["classification"] == "infra"
    assert "policy_source" not in receipt


@pytest.mark.linux_only
def test_authoritative_remote_policy_never_loads_fallback(github, monkeypatch):
    from hermes_cli import config as config_mod

    monkeypatch.setattr(
        config_mod,
        "load_config_readonly",
        lambda: (_ for _ in ()).throw(AssertionError("fallback config must not be read")),
    )
    github.update(conclusion="success", head="a" * 40)

    receipt = collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")

    assert receipt["ok"] is True
    assert "policy_source" not in receipt


@pytest.mark.linux_only
@pytest.mark.parametrize(
    "checks",
    [
        [],
        [{"context": "required", "app_id": None}],
        [{"context": "required"}],
        [{"context": " required", "app_id": 1}],
    ],
)
def test_malformed_or_unpinned_fallback_policy_fails_closed(github, monkeypatch, checks):
    from hermes_cli import config as config_mod

    monkeypatch.setattr(config_mod, "load_config_readonly", lambda: {
        "kanban": {"pr_acceptance": {"repository_policies": {
            "acme/repo": {"required_status_checks": checks},
        }}}
    })
    github.update(
        plan_gated=True,
        rules_error="Upgrade to GitHub Pro or make this repository public to enable this feature.",
        conclusion="success",
        head="a" * 40,
    )

    receipt = collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")

    assert receipt["ok"] is False
    assert receipt["classification"] == "infra"
