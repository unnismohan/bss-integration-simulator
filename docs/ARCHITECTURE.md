# Architecture

The simulator provides configurable HTTP endpoints that imitate external partner systems. A scenario defines the inbound request contract, response payloads, field mappings, and whether the interaction completes immediately or uses an ACK followed by a callback.

## Components

| Component | Responsibilities |
| --- | --- |
| React UI, served by Nginx | Scenario creation and editing, sample-based field discovery, live monitoring, and same-origin proxying for `/api` and `/sim` |
| FastAPI API | Scenario CRUD and versioning, request matching, sync response rendering, async acknowledgement and callback-job creation, health and monitoring endpoints |
| Callback worker | Claims due jobs from PostgreSQL, posts callbacks to configured destinations, and records retries and terminal outcomes |
| PostgreSQL | Persistent scenario definitions and versions, callback queue and worker control, and worker metrics |

The API and worker share the backend image and database schema but run as separate processes. The UI is built and deployed as a separate image. PostgreSQL is external to the Kubernetes starter manifests; Docker Compose includes PostgreSQL for local development.

## Request paths

### Synchronous scenario

1. A BSS client sends an HTTP request to `/sim/<scenario-key>`.
2. The API finds the configured scenario (using a short-lived in-process cache for simulation lookups), extracts configured request values, and renders the response.
3. The API returns the configured status, headers, content type, and payload directly to the client.

### ACK followed by callback

1. A BSS client sends an HTTP request to `/sim/<scenario-key>`.
2. The API captures request values, renders the configured acknowledgement and callback payload, and inserts a callback job into PostgreSQL.
3. The API returns the acknowledgement immediately.
4. The worker polls PostgreSQL, claims due jobs in batches, checks the callback host against its allowlist, and delivers the callback.
5. The worker records success or failure. Failures are retried according to the scenario's attempt count and retry delay.

PostgreSQL coordinates callback work between API and worker processes. Multiple worker replicas can claim disjoint jobs using row locking with `SKIP LOCKED`; tune replica count and batch size with the callback target's capacity in mind.

## Repository map

```text
.
├── backend/                 FastAPI service, callback worker, and backend tests
├── deploy/k8s/               Kubernetes starter manifests and deployment notes
├── docs/                     Architecture, scenario, deployment, and release docs
├── frontend/                 React UI, Nginx config, and frontend build
├── tools/                    Load generators and a callback receiver for testing
├── .github/                  CI, dependency update config, and issue templates
├── docker-compose.yml        Local multi-container environment
└── README.md                 Project overview and quick start
```

Local dependency directories, virtual environments, secrets, and validation artifacts are excluded through `.gitignore` and the Docker build contexts' `.dockerignore` files. Do not commit locally populated `.env` files or real partner payloads.

## Operational boundaries

- This application simulates configured HTTP behavior; it does not implement partner business logic or full WSDL/SOAP contract validation.
- In-process scenario caching is per API replica. Scenario edits invalidate the local cache; short cache expiry bounds staleness across replicas.
- The API key is a shared access gate, not per-user authentication or authorization.
- Callback delivery depends on network reachability and callback receiver behavior. Restrict `CALLBACK_ALLOWED_HOSTS` and egress access in shared environments.
- Database schema initialization uses SQLAlchemy `create_all`; it is not a migration framework. Use reviewed migrations before making incompatible schema changes or operating long-lived production data.
- Live process metrics are process-level measurements. Kubernetes resource metrics and cluster-wide observability should come from the platform's monitoring stack.
