# GitHub / GHCR setup

Target repository:

`spacesarmat/TGAUTOREPLY`

Container image:

`ghcr.io/spacesarmat/tgautoreply:latest`

## Required once

1. Repository `spacesarmat/TGAUTOREPLY` is the target repository.
2. Push this project to branch `main`.
3. Open **Actions** and verify **Publish GHCR image** succeeds.
4. Open the created Container package settings and set package visibility to **Public** so ZimaOS can pull it anonymously.

After that, every push to `main` rebuilds `latest` for `linux/amd64` and `linux/arm64`.
