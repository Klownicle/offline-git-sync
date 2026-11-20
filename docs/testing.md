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

Public API fixtures retain only the fields needed by the tests; repository-owner profiles and release-note bodies are not included. All network requests in those fixture tests are mocked.
