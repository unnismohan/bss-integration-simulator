# BSS Integration Simulator

An open source, configurable HTTP simulator for testing BSS integrations with external systems such as OCS, HLR/HSS, payment platforms, and ERP services. Define partner behavior in the web UI, then point a BSS integration at the simulator instead of a live partner.

**Project site:** <https://unnismohan.github.io/bss-integration-simulator/> · **Source:** <https://github.com/unnismohan/bss-integration-simulator>

## What it supports

- Synchronous request and response flows.
- Asynchronous ACK-then-callback flows with configurable delay and retries.
- JSON and XML payloads, including SOAP envelope bodies.
- Capturing values from request bodies, headers, query parameters, and URL path parameters.
- Fixed, captured, and random response values.
- Scenario version history, searchable scenario list, callback queue controls, and live process/throughput monitoring.
- PostgreSQL persistence, Docker Compose for local use, and starter Kubernetes manifests.

This project simulates HTTP partner behavior. It does not implement vendor business logic, WSDL/SOAPAction contract validation, or a production identity and authorization system.

## Project structure

- `backend/` — FastAPI API, callback worker, and tests.
- `frontend/` — React scenario builder and monitoring UI.
- `tools/` — Load-test clients and a mock callback receiver.
- `deploy/k8s/` — Kubernetes starter manifests.
- `docs/` — Architecture, scenario format, deployment, and release documentation.

See the [documentation index](docs/README.md) for details.

## Quick start with Docker Compose

Requirements: Docker Engine or Docker Desktop with Docker Compose v2.

```bash
docker compose up --build -d
```

Open the UI at <http://localhost:8088>. The API is available at <http://localhost:8000> and its development OpenAPI page at <http://localhost:8000/docs>. To stop the services, run `docker compose down`. To remove the local database volume as well, run `docker compose down -v`.

Copy `.env.example` to `.env` to customize ports, database credentials, callback allowlists, or request limits. Compose defaults are for local development only. For callbacks to services running on the host, use `host.docker.internal` where supported and allow that hostname in `CALLBACK_ALLOWED_HOSTS`.

## Configure and call a scenario

1. Open the Scenario Builder and create a scenario with a key such as `wallet-payment`.
2. Choose the HTTP method, request format, and synchronous or ACK-then-callback flow.
3. Paste representative request/response samples. Generate selectable fields and map request captures into responses.
4. Validate and save. Async scenarios need an allowed callback URL and a captured correlation value.
5. Send the BSS request to `http://localhost:8000/sim/<scenario-key>` (or the UI's same-origin `/sim/<scenario-key>` route).

See [Scenario format and examples](docs/SCENARIO_FORMAT.md) for the API contract. For load-test examples, see the scripts in `tools/`; always use synthetic or approved test data.

## Run for development

1. Copy `.env.example` to `.env` and adjust local settings if needed.
2. Start PostgreSQL with `docker compose up -d postgres`.
3. In `backend/`, create and activate a Python virtual environment, install `requirements.txt`, and start the API with `uvicorn app.main:app --reload`.
4. In a second terminal in `backend/`, start the callback worker with `python -m app.worker`.
5. In `frontend/`, run `npm ci` followed by `npm run dev`.
6. Open <http://localhost:5173>.

## Verify the source

```bash
cd backend
python -m unittest discover -s tests -v
python -m compileall -q app tests
pip check
pip-audit --requirement requirements.txt --strict

cd ../frontend
npm ci
npm audit --audit-level=high
npm run build
```

Dependency audit results change over time. Re-run them before every release and fix or document any new advisory according to your project's release policy. The container base images and operating-system packages also need scanning; application dependency audits do not cover them.

## Deploy to Kubernetes / Tanzu

Use the [Kubernetes and Tanzu deployment guide](docs/KUBERNETES_TANZU_DEPLOYMENT.md). It covers building and pushing images, secret setup, rendering the manifests, ingress/TLS, rollout verification, operations, and rollback. Manifests in `deploy/k8s/` are templates and need rendering before applying.

Before making a public GitHub repository, follow the [GitHub publication checklist](docs/GITHUB_RELEASE_CHECKLIST.md).

## Security and sample data

Do not use production credentials, subscriber identifiers, cryptographic keys, or personal data in scenarios, load tests, screenshots, or issues. The checked-in `test.xml` is synthetic and contains placeholders. Local manual-validation output under `artifacts/` is excluded from version control. Report security issues privately as described in [SECURITY.md](SECURITY.md).

For shared environments, set `APP_ENV=production`, provide a random `SIMULATOR_API_KEY` of at least 32 characters, configure TLS, use a managed PostgreSQL instance, and restrict `CALLBACK_ALLOWED_HOSTS` to approved destinations. The API key is a shared access gate, not user-level authorization. Review the operational limitations in the Tanzu guide before production use.

## Contributing

Contributions are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md), open an issue for larger changes, and run the verification commands above before submitting a pull request.

## License

This project is licensed under the [MIT License](LICENSE).
