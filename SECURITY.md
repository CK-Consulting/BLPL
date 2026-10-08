# Security policy

## Reporting a vulnerability

Please **do not** open a public issue, discussion or pull request for a
security problem.

Report it privately through GitHub:
**Security → Report a vulnerability** on
<https://github.com/CK-Consulting/BLPL/security/advisories/new>.

Include what you found, how to reproduce it, and what an attacker could do with
it. We aim to acknowledge a report within five working days and to agree a
disclosure timeline with you. We credit reporters in the advisory unless you
ask us not to.

## Supported versions

BLPL has no stable release yet. Fixes land on `main`; please test against it
before reporting.

## What is in scope

BLPL is a multi-user web app that runs LLMs over user-supplied documents and
third-party files, so the areas we care most about are:

- authentication, sessions and per-project access control;
- the at-rest sealing of project files and the key handling behind it
  (see [docs/security.md](docs/security.md) for what it does and does not
  claim to protect);
- prompt injection that leads to tool use, file access outside a project, or
  script execution in the browser;
- the quarantine path for uploaded and fetched files;
- the sandbox that confines agent file access to a project.

Findings in third-party dependencies are welcome too; we will route them
upstream.
