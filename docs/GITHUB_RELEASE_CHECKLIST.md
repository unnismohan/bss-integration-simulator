# GitHub publication checklist

This is a checklist for publishing the prepared source repository. It does not publish, push, or create a GitHub repository.

## Before creating a public repository

- Obtain copyright-holder approval for public distribution and confirm the selected license.
- Review the source, commit history, screenshots, scenario exports, and test output for confidential information, credentials, customer data, and internal hostnames.
- The local `artifacts/` directory is ignored. Keep it ignored unless all included data has been reviewed and sanitized.
- Review dependency licenses and run the backend/frontend audits. Scan built container images and operating-system packages separately.
- Choose maintainers and enable GitHub private vulnerability reporting.

## Create the repository

Suggested metadata:

- Name: `bss-integration-simulator`
- Description: `Configurable HTTP/JSON/XML partner simulator for BSS integration and load testing.`
- Topics: `telecom`, `bss`, `integration-testing`, `load-testing`, `fastapi`, `react`, `kubernetes`
- Visibility: public, after required rights-holder approval.
- Initialize without GitHub-generated README/license/gitignore files because these are already in the source tree.

If this directory is not already a Git checkout, initialize it locally, review the exact files to include, then commit and push using your normal credential manager. Never commit `.env`, private scenario exports, `artifacts/`, database dumps, or credentials.

## After the first push

- Confirm GitHub detects the MIT license and CI passes on the default branch.
- Enable secret scanning and push protection where available, Dependabot alerts, and private vulnerability reporting.
- Protect the default branch with required CI and review checks.
- Add maintainer contacts and support expectations to the repository settings.
- Create a signed/versioned release only after a clean CI run, dependency review, container scan, Tanzu deployment validation, and release notes review.
- Keep release images and Kubernetes values in an approved registry and secret manager. Do not publish private deployment values or credentials in the repository.
- GitHub Pages is deployed from `site/` by `.github/workflows/pages.yml`. Enable Pages with **Settings → Pages → Build and deployment → GitHub Actions**. The site is a project landing page; the interactive simulator API and database must be deployed separately.
