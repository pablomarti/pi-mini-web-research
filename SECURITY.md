# Security Policy

This package is designed for low-dependency, security-focused web research in Pi.

## Threat Model

Primary risks addressed:

- SSRF and local network probing from untrusted URLs
- Access to cloud metadata services
- Excessive context/token usage from unbounded tool output
- Supply-chain risk from unnecessary dependencies

## Current Controls

- `web_fetch` enforces `http`/`https` only
- Default-deny network policy for localhost/private/link-local and known metadata endpoints
- Optional allowlist via `PI_WEB_RESEARCH_ALLOWLIST` for trusted internal targets
- Tool output chunking and pagination (`offset`/`limit`) with bounds
- Local cache only (`~/.pi/agent/web-research-cache.json`), no telemetry

## Dependency Policy

- Prefer stdlib and built-in CLIs
- Small external dependencies are allowed only when:
  - pinned to an exact version
  - security-reviewed before adoption
  - justified by clear reliability or security benefit

At present, this package uses no npm runtime dependencies.

## Reporting

If you find a security issue, open a private report with:

- reproduction steps
- impact assessment
- suggested mitigation
