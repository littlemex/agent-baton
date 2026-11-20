## Purpose

<!-- Link related issues using "Fixes #123" or "Relates to #123" -->

## Changes

<!-- Summarize the changes made in this PR. For each item, prefer What / Why / Caveat / Alternative (pros and cons). -->

-

## Test Plan

<!-- Describe how you tested these changes. This repository has no infrastructure of its own
     (no Terraform/CDK/CloudFormation) - "real" here means real AWS calls (DynamoDB/IAM/STS),
     not validate/plan/mock output. -->

**Environment:**

- Python version:
- Where this ran: (the EC2 test runner, never a contributor's laptop)
- AWS resources used: (the test's own, self-created DynamoDB table and IAM roles - named, and confirmed deleted afterward)

**Test commands:**

```bash

```

## Test Results

<!--
REQUIRED for functional changes: paste the pytest output, including the AWS-marked
integration tests where the change touches ledger.py/aws_setup.py, and confirmation
that every resource the test run created was deleted (see tests/conftest.py).

For changes with no functional impact, replace this section's content with
the single word: docs-only
-->

## Directory Structure

<!-- If adding or updating a module, ensure it follows the expected layout below. -->

```text
agent_baton/        # the library + CLI (ledger.py, policy.py, crypto.py, aws_setup.py, cli.py)
tests/               # pytest; AWS-marked tests create and delete their own table/roles
Dockerfile           # builds the CLI image; no Terraform/CDK/CloudFormation in this repo
```

- Not all directories are touched by every change - include only the ones relevant to your change.

## Checklist

- [ ] I have read the [contributing guidelines](https://github.com/littlemex/agent-baton/blob/main/CONTRIBUTING.md) and `CONTRACT.md`.
- [ ] I am working against the latest `main` branch.
- [ ] I have searched existing open and recently merged PRs to confirm this is not a duplicate.
- [ ] The contribution is self-contained with documentation and tests.
- [ ] External dependencies are pinned to a specific version (no unpinned `latest`).
- [ ] A README is included or updated with prerequisites, instructions, and known issues.
- [ ] New test cases follow the [expected directory structure](#directory-structure).
- [ ] Test Results above contain real-AWS evidence (or this PR is marked `docs-only`); every resource the test run created is confirmed deleted.
