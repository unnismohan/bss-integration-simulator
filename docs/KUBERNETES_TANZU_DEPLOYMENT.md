# BSS Integration Simulator: Team Release and Tanzu Deployment Guide

**Status:** First release guide
**Audience:** Development, QA, platform, and operations teams
**Scope:** Build and publish the simulator images, deploy the service to a Tanzu Kubernetes cluster, verify it, and roll a release back.

This guide is based on the repository's current Dockerfiles and the starter manifests in `deploy/k8s`. Treat the values below as examples. Replace registry names, namespace, hostnames, database details, TLS secret, resource sizing, and network policy with values approved for your environment.

## 1. What is being deployed

The simulator has three application workloads and an external PostgreSQL dependency:

| Workload | Purpose | Kubernetes service/deployment |
| --- | --- | --- |
| API | Scenario management, synchronous simulation, async ACK creation, metrics and health endpoints | `bss-simulator-api` Deployment and Service |
| Callback worker | Delivers queued async callbacks, records retry/success/failure status | `bss-simulator-callback-worker` Deployment |
| UI | Scenario Builder and Live Monitoring, served by Nginx | `bss-simulator-ui` Deployment and Service |
| PostgreSQL | Scenario definitions/versions, callback queue, worker control and service metrics | Managed/approved PostgreSQL outside the manifests |

The API and callback worker use the **same backend image**, with a different container command. The UI is a separate image. The UI uses same-origin `/api` and `/sim` routes, which Nginx and the Ingress route to the API. Expose the UI through TLS; do not publish the database or API service directly to untrusted networks.

The API starts with two replicas in the sample Deployment and can autoscale from two to eight replicas by CPU. The worker starts with one replica. PostgreSQL is not deployed by these files: use the platform's approved database service, backups, monitoring, and TLS configuration.

## 2. Release prerequisites

Before building, arrange the following with the platform team:

- A Tanzu namespace, resource quota, and deployment identity with permission to manage Deployments, Services, ConfigMaps, Secrets, Ingresses, and HPAs in that namespace.
- A private OCI image registry reachable from the build host and cluster nodes. Configure `imagePullSecrets` or the namespace's default ServiceAccount if the registry requires authentication.
- An approved PostgreSQL instance accessible from the namespace. The API needs database access at startup and during readiness checks; the worker needs it to claim and update callback jobs.
- DNS and an ingress class for the application hostname, plus a TLS Secret in the target namespace. The sample Ingress may need `spec.ingressClassName` set to the class selected by your Tanzu platform.
- Approved callback destinations and egress firewall rules. Callback URLs must resolve and be reachable from the worker Pods. Only list hostnames that the test environment is permitted to call.
- An approved secret-management method for the database URL and API key. Do not put credentials in Git, ConfigMaps, shell scripts, image layers, or release artifacts.
- A build host with Docker Buildx, registry credentials, `kubectl`, `envsubst` (from `gettext`), and access to the Kubernetes cluster.

Confirm the cluster context and namespace before applying anything:

```bash
kubectl config current-context
kubectl get nodes
kubectl get namespace
```

## 3. Build and publish release images

Use an immutable release identifier. A Git commit SHA is recommended; do not reuse `latest` or overwrite an existing release tag. The example below builds images for Linux AMD64. Confirm the Tanzu node architecture with the platform team before choosing `--platform`.

```bash
export REGISTRY=registry.example.net/team
export RELEASE_TAG=2026.10.01-a1b2c3d
export BACKEND_IMAGE="$REGISTRY/bss-simulator-backend:$RELEASE_TAG"
export UI_IMAGE="$REGISTRY/bss-simulator-ui:$RELEASE_TAG"

docker login registry.example.net
docker buildx inspect --bootstrap

docker buildx build \
  --platform linux/amd64 \
  --pull \
  --provenance=true \
  --sbom=true \
  --tag "$BACKEND_IMAGE" \
  --push ./backend

docker buildx build \
  --platform linux/amd64 \
  --pull \
  --provenance=true \
  --sbom=true \
  --tag "$UI_IMAGE" \
  --push ./frontend
```

Both Dockerfiles run dependency checks during the build: `pip-audit --strict` for Python requirements and `npm audit --audit-level=high` for frontend dependencies. A failed audit should block release until the advisory is reviewed and the dependency is upgraded, replaced, or explicitly handled under the project's release policy. These checks cover package advisories; they do not guarantee a vulnerability-free image or scan all base-image/OS packages. Run a registry or container scanner, review the results, and record the image digests in the release record.

Verify that each image exists in the registry and record its digest. Use an image signing and provenance process if required. Store the exact image references (preferably digests) with the release record so the same artifacts can be rolled back to.

## 4. Prepare the namespace and secrets

Create or select the namespace using the platform process. The examples use `bss-simulator`; replace it with your approved namespace.

```bash
export NAMESPACE=bss-simulator
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
```

Create these Secrets using the approved secret manager or platform workflow:

### `bss-simulator-database`

Must contain the key `url`. The value is a SQLAlchemy URL using the `postgresql+psycopg` driver. Example shape:

```text
postgresql+psycopg://simulator_user:REPLACE_WITH_SECRET@postgres.example.net:5432/simulator
```

Percent-encode reserved URL characters in the username/password. Ensure the database/network policy permits connections from the namespace and that backups and restore have been tested. The application uses this URL for synchronous `psycopg` connections and derives an `asyncpg` URL from it for asynchronous database work. TLS query options can differ between those drivers; do not add a driver-specific option such as `sslmode` to this shared URL without confirming it works for both. If policy requires verified TLS for both drivers, confirm the current application has driver-specific SSL settings and mounted CA configuration before release; add that support if it does not.

### `bss-simulator-auth`

Must contain the key `api-key`. In `APP_ENV=production`, the API requires `SIMULATOR_API_KEY` to be at least 32 characters. Use a randomly generated key stored by your approved secret manager. The UI prompts for the key and keeps it in that browser session; provide it to authorized test users through the secret-management process.

Example Secret creation for a non-production namespace only (use secret manager commands for shared/prod environments; never commit the generated YAML):

```bash
kubectl create secret generic bss-simulator-database \
  --namespace "$NAMESPACE" \
  --from-literal=url='postgresql+psycopg://simulator_user:REPLACE_WITH_SECRET@postgres.example.net:5432/simulator'

kubectl create secret generic bss-simulator-auth \
  --namespace "$NAMESPACE" \
  --from-literal=api-key='REPLACE_WITH_RANDOM_SECRET_OF_AT_LEAST_32_CHARACTERS'
```

If the secret already exists, update it through the approved secret manager instead of deleting and recreating it during a production rollout.

If the registry is private, configure a registry pull Secret via the namespace's approved ServiceAccount or add `imagePullSecrets` to each Pod template. Confirm the pull secret is available in this namespace before rollout.

## 5. Configure the deployment

The checked-in manifests are templates and contain shell-style `${VARIABLE}` placeholders. Render them with `envsubst`; do not apply the template files directly. Choose values that fit the database and node capacity.

```bash
export BACKEND_IMAGE=registry.example.net/team/bss-simulator-backend:2026.10.01-a1b2c3d
export UI_IMAGE=registry.example.net/team/bss-simulator-ui:2026.10.01-a1b2c3d
export APP_HOST=simulator.test.example.net
export TLS_SECRET_NAME=simulator-test-tls
export CORS_ORIGINS="https://$APP_HOST"
export CALLBACK_ALLOWED_HOSTS="bss-test.example.net,callback-mock.test.example.net"
```

Configuration notes:

| Setting | Guidance |
| --- | --- |
| `APP_ENV` | Keep `production` for shared/official environments. It enforces the API key and disables API docs by default. |
| `ENABLE_API_DOCS` | Keep `false` in production unless explicitly approved. |
| `CALLBACK_ALLOWED_HOSTS` | Comma-separated hostnames only: no scheme, path, or port. Include only callback hosts the simulator is authorized to contact. Applies to the callback worker and API. |
| `CORS_ORIGINS` | Comma-separated browser origins including scheme, e.g. `https://simulator.test.example.net`; no path or trailing slash. |
| `CALLBACK_BATCH_SIZE` | Maximum callback jobs claimed per worker batch. The worker drains additional batches while work remains. Increasing it increases concurrent callback connections and database writes. |
| `CALLBACK_POLL_SECONDS` | Idle polling interval when no work is found. |
| `API_DB_POOL_SIZE`, `API_DB_MAX_OVERFLOW` | Per API Pod connection capacity. The example sets 15 + 5 per API Pod. Budget total connections across max API replicas, worker Pods, migrations, monitoring and administrators. |
| `MAX_REQUEST_BYTES` | Maximum inbound simulation request body; sample is 1 MiB. Increase only for known test payload requirements. |

The sample API pool can consume up to `(15 + 5) × 8 = 160` PostgreSQL connections at the HPA maximum, before worker and operational connections. The worker overrides its pool to 5 + 5 per Pod. Verify `max_connections` and reserve capacity for normal operations before scaling replicas/pools. Adjust HPA maximum, pools, and PostgreSQL limits together.

The HPA requires the cluster metrics API. The sample CPU target of 70% and resource requests/limits are starting points, not a sizing guarantee. Run a representative load test in the target cluster and tune against CPU, memory, DB latency, connection use, callback backlog, and observed latency.

## 6. Render and apply the Kubernetes manifests

Apply in order: ConfigMap and API, wait for API readiness, then worker/UI and Ingress. The readiness endpoint checks PostgreSQL, so the API will not become Ready until database connectivity succeeds.

```bash
set -euo pipefail
RENDER_DIR="$(mktemp -d)"
trap 'rm -rf "$RENDER_DIR"' EXIT

for manifest in config.yaml api.yaml worker.yaml ui.yaml ingress.yaml; do
  envsubst < "deploy/k8s/$manifest" > "$RENDER_DIR/$manifest"
done

# Ask the cluster API/admission policies to validate the rendered objects first.
for manifest in config.yaml api.yaml worker.yaml ui.yaml ingress.yaml; do
  kubectl apply --dry-run=server --namespace "$NAMESPACE" -f "$RENDER_DIR/$manifest"
done

kubectl apply --namespace "$NAMESPACE" -f "$RENDER_DIR/config.yaml"
kubectl apply --namespace "$NAMESPACE" -f "$RENDER_DIR/api.yaml"
kubectl rollout status --namespace "$NAMESPACE" deployment/bss-simulator-api --timeout=5m

kubectl apply --namespace "$NAMESPACE" -f "$RENDER_DIR/worker.yaml"
kubectl apply --namespace "$NAMESPACE" -f "$RENDER_DIR/ui.yaml"
kubectl apply --namespace "$NAMESPACE" -f "$RENDER_DIR/ingress.yaml"
```

The Ingress file contains both the Ingress and HPA YAML documents; one `kubectl apply` applies both. Confirm that all resources were created:

```bash
kubectl get deployment,service,ingress,hpa,configmap -n "$NAMESPACE"
```

Set `spec.ingressClassName` to the ingress class supported by your Tanzu cluster if the controller does not select it by default. Confirm the TLS Secret exists in the same namespace before expecting HTTPS to work.

## 7. Verify the release

### Pod rollout and logs

```bash
kubectl get pods -n "$NAMESPACE" -o wide
kubectl rollout status -n "$NAMESPACE" deployment/bss-simulator-api --timeout=5m
kubectl rollout status -n "$NAMESPACE" deployment/bss-simulator-callback-worker --timeout=5m
kubectl rollout status -n "$NAMESPACE" deployment/bss-simulator-ui --timeout=5m
kubectl logs -n "$NAMESPACE" deployment/bss-simulator-api --tail=100
kubectl logs -n "$NAMESPACE" deployment/bss-simulator-callback-worker --tail=100
kubectl describe hpa -n "$NAMESPACE" bss-simulator-api
```

### Health and UI

```bash
curl -fsS "https://$APP_HOST/health/live"
curl -fsS "https://$APP_HOST/health/ready"
```

Open `https://$APP_HOST/` and enter the API key when prompted. Verify the scenarios list, Scenario Builder, and Live Monitoring. Health endpoints are unauthenticated so Kubernetes probes can use them; `/api` and `/sim` require the API key in production.

### Verify OCS path-parameter simulation

The OCS example scenario uses the scenario key `ocs` and path template `/subscriber/{msisdn}`. Test a representative request:

```bash
curl -i -X GET \
  "https://$APP_HOST/sim/ocs/subscriber/243810123456" \
  -H "Accept: application/json" \
  -H "X-Correlation-Id: OCS-REQ-RELEASE-CHECK-000001" \
  -H "X-Simulator-Api-Key: $SIMULATOR_API_KEY"
```

The sample is a GET with no body, so `Content-Type` is not required. Confirm HTTP 200, JSON content type, and that `subscriber.msisdn` in the response matches the URL segment.

### Load test from a workstation

Run a small baseline first, then increase the rate gradually. Provide `SIMULATOR_API_KEY` through your approved secret mechanism when testing a production-mode environment. The load driver reads that environment variable and sends it as `X-Simulator-Api-Key` when present.

```bash
python3 tools/load_ocs_test.py \
  --url "https://$APP_HOST/sim/ocs/subscriber/{msisdn}" \
  --tps 50 --duration 30 --concurrency 200
```

Then use a stepped profile such as 100 TPS, 150 TPS, and the agreed target. Review completed/scheduled requests, skipped requests, non-2xx/network failures, p95/p99 latency, API CPU/memory, database connections, HPA activity, and callback queue depth. Avoid running the performance test through the Scenario Builder UI; use the script or actual BSS traffic. Record client location and whether requests used the Ingress, a port-forward, or an in-cluster load generator because each exercises different network paths.

## 8. Operational and security notes

- Put the service behind an identity provider or SSO gateway and TLS before shared user access. The API key is a shared key, not individual identity, role-based authorization, or an audit trail. Rotate it using the secret-management process.
- The callback worker makes outbound HTTP(S) calls. Use the host allowlist, egress controls, DNS policy, and a controlled callback test receiver. Do not allow arbitrary callback hosts.
- Keep database and API credentials out of image build arguments and ConfigMaps. Restrict Secret access via RBAC and use the cluster's encryption-at-rest/secret integration.
- Current application startup creates tables through SQLAlchemy metadata. There is no versioned migration job in this repository yet. For an official production release, review the schema/bootstrap behavior with the DBA, take a database backup, and define restore and schema-change procedures before upgrades.
- Callback job rows and scenario versions persist in PostgreSQL. Establish a reviewed retention/archival policy and monitor database growth; do not delete operational history ad hoc.
- Set CPU/memory requests and limits to match measured usage and namespace quotas. Keep the API and worker DB pool budgets aligned with maximum replica counts.
- The manifests are starter examples, not a complete production baseline. Add environment NetworkPolicies, PodDisruptionBudgets, policy labels, ingress annotations, image pull configuration, observability, and secret integration as required.

## 9. Release rollback

Keep the previous image tag/digest and deployment configuration in the release record. Roll back the application images without rolling back PostgreSQL data:

```bash
kubectl rollout history -n "$NAMESPACE" deployment/bss-simulator-api
kubectl rollout undo -n "$NAMESPACE" deployment/bss-simulator-api
kubectl rollout undo -n "$NAMESPACE" deployment/bss-simulator-callback-worker
kubectl rollout undo -n "$NAMESPACE" deployment/bss-simulator-ui
kubectl rollout status -n "$NAMESPACE" deployment/bss-simulator-api --timeout=5m
```

For immutable tagged releases, re-render/apply the prior image references if rollout history does not contain the desired version. Do not restore or modify the database as part of an application rollback unless the DBA-approved recovery plan specifically requires it. Recheck health, UI access, simulation responses, and callback delivery after rollback.

## 10. Release checklist

- [ ] Source revision and release tag recorded; images are immutable and scanner-reviewed.
- [ ] Backend and UI image digests recorded and available to the Tanzu cluster.
- [ ] Namespace quota, registry pull access, ingress class, TLS, DNS, and network policies confirmed.
- [ ] PostgreSQL connectivity, credentials, connection budget, backup, and restore plan confirmed.
- [ ] `bss-simulator-database` and `bss-simulator-auth` secrets created through approved secret management.
- [ ] `CALLBACK_ALLOWED_HOSTS` and `CORS_ORIGINS` match the release environment.
- [ ] Rendered manifests reviewed; image references and hostname/TLS values verified before apply.
- [ ] API, worker, UI, Ingress, and HPA are Ready; health endpoints return success.
- [ ] Authorized user can sign in to the UI with the API key and run a sample simulation.
- [ ] Representative load test completed and latency/errors/resources/database/callback behavior reviewed.
- [ ] Release notes, known limitations, rollback image references, and operator contact recorded.
