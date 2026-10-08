# Security policy

## Supported version

Security fixes are currently accepted for the latest code on the default branch. There are no separately maintained release branches yet.

## Report a vulnerability

Please do not report suspected vulnerabilities in public issues. Use GitHub's **Report a vulnerability** feature for this repository's Security tab when it is enabled. If private vulnerability reporting is unavailable, contact the repository maintainers privately and include a description, impact, reproduction steps, and affected version. Do not include real credentials or customer data in the report.

The maintainers will acknowledge reports as soon as practical, assess severity and affected versions, and coordinate a fix and disclosure timeline with the reporter.

## Deployment security

- Use TLS and a managed PostgreSQL service with backups for shared environments.
- Set `APP_ENV=production` and a random API key of at least 32 characters.
- Keep callback host allowlists narrow; the callback worker makes outbound HTTP requests to those hosts.
- Do not expose development Compose credentials or unrestricted database/API ports.
- Scan container images, including base image and OS packages, in the target registry.
