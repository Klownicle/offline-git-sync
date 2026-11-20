# Running the tests

Run these commands from the application folder on Windows with Python 3.11 or newer:

```powershell
python -m unittest discover -s tests
```

The tests use temporary folders, mocked remote responses, and mocked scheduled-task operations. HTTP-handler tests start disposable servers on `127.0.0.1`; filesystem tests require ordinary directory access and permission to create hard links. Run them in a normal local environment that permits these operations. A sandbox that denies loopback connections or directory resolution cannot run the complete suite successfully.

The optional JavaScript tests use only Node.js built-in modules. Install [Node.js](https://nodejs.org/) to run them; Node is not an application dependency. The tests have been run with Node.js 24.

```powershell
Get-ChildItem .\tests\test_*.js | ForEach-Object {
    node $_.FullName
    if ($LASTEXITCODE -ne 0) { throw "Failed: $($_.Name)" }
}
```

The JavaScript tests exercise dashboard logic with isolated DOM and API mocks. They do not require a browser or make changes to saved syncs.

Release-selection coverage includes preservation of legacy settings, repository-specific type classification, shared and separate limits, pagination, and Asset Preview consistency. `test_release_catalog.py` checks remembered type metadata, and `test_add_review.py` checks the read-only Add review and confirmation token. `test_release_refresh.py` checks preview-bound deletion and worker safeguards, while `test_refresh_progress.py` covers refresh-specific progress labels. `test_release_channel_ui.js` covers dynamic type discovery and grouping, future-type selection behavior, archived settings, Add review/confirmation, unsaved-edit guards, and refresh progress. These tests use the same commands and dependencies above.

`test_delete_sync_ui.js` covers the unchecked file-deletion default, both preview modes, explicit confirmation, stale-preview rejection, exact path display, and partial-failure reporting. The new-sync UI tests also verify the 240-minute default without changing existing schedule values.

`test_delete_sync.py` covers task ownership, bounded folder deletion, stale previews, partial failures, and retry behavior. `test_deleted_sync.py` checks queued-task protection and the CLI interval default. `test_delete_lifecycle_review.py` verifies that an old deletion confirmation cannot remove a newly re-added sync. The HTTP tests cover both preview modes and truthful partial-failure responses.

Public API fixtures retain only the fields needed by the tests; repository-owner profiles and release-note bodies are not included. All network requests in those fixture tests are mocked.
