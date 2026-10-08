# Contributing to BLPL

Thanks for helping. BLPL is young and moving quickly, so the most useful thing
you can do before writing code is open an issue describing what you want to
change and why — it saves you building something that collides with work in
flight.

By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Ways to help

- **Report a bug** — use the bug report form. A minimal design document that
  reproduces it is worth more than any description.
- **Fix a symbol, footprint or pin type** — library corrections are among the
  most valuable changes there are. Say which datasheet section the fix comes
  from.
- **Improve the docs** — if something in [docs/](docs/) was wrong or missing
  for you, it is wrong or missing for the next person too.
- **Code** — anything labelled `good first issue` or `help wanted` is open.

## Setting up

See [docs/install.md](docs/install.md). In short:

```bash
git clone --recurse-submodules https://github.com/CK-Consulting/BLPL.git
cd BLPL
python -m pip install -e '.[dev]'
python -m pytest tests/ -q
cd app/frontend && npm ci && npx vitest run && npm run build
```

`kicad-symbols` and `kicad-footprints` are large. For the test suite a shallow
checkout of just those two is enough:
`git submodule update --init --depth 1 kicad-symbols kicad-footprints`.
Without them about 46 emitter and library tests fail; that is the environment,
not your change.

## Making a change

1. Fork the repository and create a branch from `main`.
2. Keep the change focused. Two unrelated fixes are two pull requests.
3. Add or update tests. A bug fix includes a test that fails without the fix.
4. Run the Python and frontend suites locally.
5. Open a pull request using the template, and link the issue it addresses.

Pull requests are reviewed by a maintainer, and also by an automated reviewer.
Address or answer every review comment; a pull request with open review
comments is not merged.

### Style

- Match the code around you — naming, comment density, idiom.
- Comments explain *why*. Many in this codebase record what went wrong before
  a change; keep that habit.
- Commit messages describe what changed and why, in prose. A one-line subject,
  a blank line, then the reasoning.
- KiCad files are parsed and rewritten through `blpl/emitter/sexpr.py`, never
  edited by hand or by regex.

### What not to commit

- **Third-party documents** — datasheets, application notes, vendor 3D models,
  reference designs. Most are not ours to redistribute. Reference them by URL.
- **Secrets or personal paths** — nothing from `.env`, no tokens, no absolute
  paths from your machine.
- **Generated output** — `.pipeline/` and build artifacts.

## Sign your work (DCO)

Every commit must carry a `Signed-off-by` line certifying the
[Developer Certificate of Origin](https://developercertificate.org/): that you
wrote the change, or otherwise have the right to submit it under this
project's license.

```bash
git commit -s -m "Explain the change"
```

## AI-assisted contributions

Contributions written with AI tools are welcome, on the same terms as any
other:

- **Say so.** Note in the pull request description which tools you used and
  for what.
- **You are the author.** You have read, understood and tested every line you
  submit, and you can answer review questions about it. "The model wrote it"
  is not an answer to a review comment.
- **The DCO still applies.** Do not submit generated code you have reason to
  believe reproduces someone else's copyrighted work.

[AGENTS.md](AGENTS.md) orients coding agents to this repository. Keep personal
agent configuration in `.claude/`, `CLAUDE.local.md` or `AGENTS.local.md`,
which are gitignored.

## License

BLPL is licensed under the [Apache License 2.0](LICENSE). By contributing, you
agree that your contributions are licensed under the same terms (Apache-2.0,
section 5).

## Security issues

Do not open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).
