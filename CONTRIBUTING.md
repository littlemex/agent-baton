# Contributing Guidelines

Thank you for your interest in contributing to this project. Whether it's a bug report, new
feature, correction, or additional documentation, we greatly value feedback and contributions
from our community.

Please read through this document, and `CONTRACT.md`, before submitting any issues or pull
requests to ensure we have all the necessary information to effectively respond to your bug
report or contribution.

## Reporting Bugs/Feature Requests

We welcome you to use the GitHub issue tracker to report bugs or suggest features.

When filing an issue, please check existing open, or recently closed, issues to make sure
somebody else hasn't already reported the issue. Please try to include as much information as
you can. Details like these are incredibly useful:

* A reproducible test case or series of steps.
* The commit or tag of this repository being used.
* Any modifications you've made relevant to the bug.
* Anything unusual about your environment (how you run the Broker: library, CLI, or the
  Docker image).

Please redact AWS account IDs, role ARNs and credentials from anything you paste.

## Contributing via Pull Requests

Contributions via pull requests are much appreciated. Before sending us a pull request, please
ensure that:

1. You are working against the latest source on the *main* branch.
2. You check existing open, and recently merged, pull requests to make sure someone else
   hasn't addressed the problem already.
3. You open an issue to discuss the work first. Every pull request links an issue, and `main`
   accepts changes only through pull requests.
4. A change to the Ledger's state machine, permission model, or API shape (anything
   `CONTRACT.md` describes) updates `CONTRACT.md` in the same pull request - the contract and
   the implementation never drift apart.

To send us a pull request, please:

1. Fork the repository.
2. Modify the source; please focus on the specific change you are contributing. If you also
   reformat all the code, it will be hard for us to focus on your change.
3. Add or update tests. A test that calls real AWS (DynamoDB/IAM/STS) is marked
   `@pytest.mark.aws`, creates its own resources with a unique prefix, and tears them down
   (see `tests/conftest.py`) - run it only on a dedicated test runner, never on your laptop.
4. Commit to your fork using clear commit messages.
5. Send us a pull request, answering the questions in the pull request template.
6. Pay attention to any automated CI failures reported in the pull request, and stay involved
   in the conversation.

GitHub provides additional documentation on
[forking a repository](https://help.github.com/articles/fork-a-repo/) and
[creating a pull request](https://help.github.com/articles/creating-a-pull-request/).

## Contributions format

Regardless of their nature, components of this repository must:

* Be self contained with documentation and tests. If they depend on external software
  (libraries, base images), fix the versions via a version, tag, or digest to ensure
  reproducibility and do not use a `latest` tag.
* Have a README, or a section of the root README, with the prerequisites and instructions for
  running or testing the component.
* Follow a KISS principle, **K**eep **I**t **S**imple and **S**tupid. Users should expect to
  copy paste commands, reproduce tests as is, and have some guidance on what to modify if they
  need to.
* Be contributed through pull requests, without exception.
* Be verified against real AWS (DynamoDB/IAM/STS) for any change to `ledger.py` or
  `aws_setup.py` - validate/plan/mock output is not accepted as verification.

This repository ships no infrastructure-as-code of its own (no Terraform, CDK, or
CloudFormation) - see the root README for what a caller needs to provide to run this (a
DynamoDB table of the documented schema, and IAM roles/policies of the documented shape).

## Finding contributions to work on

Looking at the existing issues is a great way to find something to contribute on. Issues
labeled `help wanted` are a great place to start.

The repository uses the default GitHub labels plus the following:

| Label | Meaning |
| --- | --- |
| `RFC` | Proposal for a significant change to the contract (API, state machine, permission model) |
| `ci-failure` | A CI workflow is failing |
| `security` | Security hardening or a vulnerability |
| `stale` | No activity; set by the stale workflow |

## Code of Conduct

See [CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md).

## Security issue notifications

If you discover a potential security issue in this project we ask that you report it privately
as described in [SECURITY.md](./SECURITY.md). Please do **not** create a public GitHub issue.

## Licensing

See the [LICENSE](LICENSE) file for our project's licensing. By submitting a pull request, you
agree that your contribution is licensed under the same Apache-2.0 License.
