# Charm release process

The release workflow publishes the exact integration-tested charm artifact.
It does not rebuild the charm during release.

## Test an exact commit

Run `Charm Integration` from the repository `main` branch.
Provide the full commit SHA in `source_sha`.

The workflow performs these steps:

1. It checks out the exact commit.
2. It builds one Ubuntu 24.04 charm artifact.
3. It deploys that artifact to a temporary model.
4. It runs the regtest integration contract.
5. It uploads the tested artifact under a commit-bound name.

Record the successful workflow run ID.
Download the artifact and calculate its SHA-256 digest.

```bash
sha256sum bitcoin-rpc_ubuntu@24.04-amd64.charm
```

## Publish the tested artifact

Run `Release Tested Charm` from `main`.
Provide the source SHA, integration run ID, artifact digest, and channel.

The workflow enforces these gates:

- the repository and workflow branch are canonical;
- the source commit belongs to `main`;
- source tests pass again at that commit;
- the downloaded artifact name includes the commit;
- the artifact digest matches the operator-provided digest;
- the `charmhub-release` environment approves publication.

The workflow uploads the existing artifact once.
It releases that exact Charmhub revision to the selected channel.
It then verifies the published revision in Charmhub status.

The workflow also creates a GitHub provenance release.
That release records the source SHA and artifact digest.

Do not use the legacy proxy release workflow for charm publication.
That workflow publishes the Go RPC proxy release assets.
