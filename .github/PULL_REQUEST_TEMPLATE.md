## What and why

<!-- What does this change, and what problem does it solve? Link the issue: "Fixes #123". -->

## How it was tested

<!-- Tests added or changed. For a bug fix: the test that fails without it. -->

- [ ] The suite of every project it touches passes: `cd core && python -m pytest -q`, `cd plugin && python -m pytest -q`, `cd app/backend && python -m pytest tests -q`
- [ ] No commit touches more than one of `core/`, `plugin/` and `app/`
- [ ] `cd app/frontend && npx vitest run && npm run build` passes (if the frontend changed)

## Checklist

- [ ] Commits are signed off (`git commit -s`) — see CONTRIBUTING.md
- [ ] No third-party documents (datasheets, vendor models), secrets or personal paths
- [ ] Docs updated if behaviour changed

## AI assistance

<!-- Which tools, if any, and for what. You remain responsible for every line. Write "none" if none. -->
