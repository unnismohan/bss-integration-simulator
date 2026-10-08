# Tanzu deployment files

These are starter manifests for the BSS Integration Simulator. They use `${...}` template variables and must be rendered with `envsubst` before applying. Required variables and the full image-release, secret setup, deploy, verification, load-test, security, and rollback procedures are documented in [the Kubernetes and Tanzu deployment guide](../../docs/KUBERNETES_TANZU_DEPLOYMENT.md).

The database is external to this directory. Create the `bss-simulator-database` and `bss-simulator-auth` Secrets in the target namespace through the approved secret-management process before deploying the API. Adapt ingress class, image-pull configuration, resources, policies, and callback allowlists to your Tanzu environment.
