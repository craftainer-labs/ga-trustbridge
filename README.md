# trustbridge

> [!NOTE]
> This repository is vibe coded with [Claude](https://claude.com/product/claude-code).

A self-hosted, OIDC-first GitHub App that provisions registry and GitOps
access for repositories. The backend of the `trustbridge` GitHub App,
built from [`craftainer/template-fastapi`](https://github.com/craftainer/template-fastapi).
See [`docs/TEMPLATE.md`](docs/TEMPLATE.md) for contents, getting started,
checks, template sync, versions/config, and code style — everything
about this repository that's identical across every instance of this
template. `docs/TEMPLATE.md` stays as-is and is what
`.github/workflows/template-sync.yml` keeps up to date.

## Operating

Run it on Kubernetes with the Helm chart in [`deploy/helm/`](deploy/helm/README.md):

- [Install](docs/install.md)
- [Upgrade](docs/upgrade.md)
- [Backup and restore](docs/backup-restore.md)
- [Air-gap, CA bundle and proxy](docs/airgap-ca-proxy.md)

## License

Licensed under the [Apache License 2.0](LICENSE).
