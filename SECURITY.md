# Security Policy

## Reporting a Vulnerability

If you believe you've found a security vulnerability in this repository,
please report it privately using GitHub's
[private vulnerability reporting](https://github.com/tekflox/aw-app-marketing/security/advisories/new)
feature rather than opening a public issue.

We'll acknowledge your report and follow up with next steps as soon as
possible.

## Supported Versions

Only the latest version on `master` is supported. There are no maintained
release branches.

## Credentials in this repo

There are none, and there must never be any: this repo is **public**, and its
security scan (`.github/workflows/security-scan.yml`) runs Gitleaks over the
git **history**, so a token committed once and removed in the next commit is
still a leaked token.

The two credentials this app touches — the workspace API key and the Meta
access token — live only in the generated `mcp.json`, which is gitignored and
written `0600` at runtime. The Meta token itself is stored in the workspace's
`config_store`, outside the package dir entirely. Test fixtures use the
obvious placeholder `sk-test-not-a-real-token` and assert on shape.
