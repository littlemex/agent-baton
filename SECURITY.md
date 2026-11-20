# Security Policy

## Reporting a Vulnerability

Please report security vulnerabilities privately through
[GitHub private vulnerability reporting](https://github.com/littlemex/agent-baton/security/advisories/new).
Do **not** open a public issue, pull request or discussion for a vulnerability.

Include, as far as you can:

- The affected file or function, and the commit or tag.
- The impact: what an attacker can read, change, claim, or reach (for example: write the
  Ledger table directly, bypass the approval gate, or obtain work credentials wider than a
  profile grants).
- Steps to reproduce, with AWS account IDs, role ARNs and other sensitive values redacted.

If the vulnerability is in an AWS service rather than in this repository, report it to AWS
through the [AWS vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/).

## Scope

This project is a library and CLI for a crash-safe task ledger backed by DynamoDB, IAM and
STS. It ships no infrastructure-as-code of its own (see README "What this needs to run") -
findings in scope are in the library's own logic and in the documented minimal-permission
examples, for example:

- A way for the Ledger's state machine (`claim()`, the two-call wait, the approval gate) to
  be bypassed, double-claimed, or made to skip the retry/deadline accounting described in
  `CONTRACT.md`.
- A way for a caller without the broker's own credentials to write the Ledger table.
- A way for `vend_work_credentials()` to hand out permissions wider than the target role's
  own identity policy intersected with the profile's `deny_statements`.
- Credentials or sensitive values written to logs or exceptions.
- An unpinned dependency, base image, or GitHub Actions step usable to inject code.

Out of scope: the MFA- and allow-list-gated API front door and the Kueue/resource-scheduler
adapter described in `CONTRACT.md` as not implemented in v1 - there is no code here to find
a vulnerability in yet.

## Supported Versions

Security fixes are made on `main` and released in the next tag.
