# Contributing

Thanks for considering a contribution.

## Before you start

- Search existing issues and pull requests for related work.
- Open an issue before a large change so the approach can be discussed.
- Never include production credentials, private endpoint addresses, personal data, or real subscriber keys in source, tests, logs, screenshots, or sample scenarios. Use synthetic fixtures.

## Development workflow

1. Fork the repository and create a focused branch.
2. Make the smallest change that addresses the issue and add or update tests.
3. Run backend checks from `backend/`:

   ```bash
   python -m unittest discover -s tests -v
   python -m compileall -q app tests
   pip check
   pip-audit --requirement requirements.txt --strict
   ```

4. Run frontend checks from `frontend/`:

   ```bash
   npm ci
   npm audit --audit-level=high
   npm run build
   ```

5. Update documentation when configuration or user-visible behavior changes.
6. Submit a pull request with the problem, approach, test results, and any deployment or migration notes.

## Pull request expectations

- Keep changes scoped and explain trade-offs.
- Do not commit `.env` files, generated build output, local validation artifacts, or secrets.
- Keep dependency changes pinned and include their audit results and reason for the change.
- Treat database schema changes as migrations; the current startup `create_all` behavior is not a migration system.

By submitting a contribution, you agree that it is provided under the repository's MIT license and that you have the right to submit it.
