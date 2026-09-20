# v1.5.1 — GitHub Actions на Node 24

Технический релиз обновляет pipeline сборки и публикации Docker-образа в GHCR.

## Что изменено

- `actions/checkout@v4` → `actions/checkout@v5`
- `docker/setup-qemu-action@v3` → `docker/setup-qemu-action@v4`
- `docker/setup-buildx-action@v3` → `docker/setup-buildx-action@v4`
- `docker/login-action@v3` → `docker/login-action@v4`
- `docker/metadata-action@v5` → `docker/metadata-action@v6`
- `docker/build-push-action@v6` → `docker/build-push-action@v7`

Все перечисленные Actions используют runtime Node 24. Предупреждение GitHub о deprecated Node 20 больше не должно появляться от этих шагов.

## Что осталось без изменений

- публикация `ghcr.io/spacesarmat/tgautoreply`;
- `latest` при push в `main`;
- version tag при push `v*`;
- `sha-*` tag;
- сборка `linux/amd64` и `linux/arm64`;
- GitHub Actions cache для BuildKit;
- авторизация GHCR через стандартный `GITHUB_TOKEN`.

## Обновление на ZimaOS

После успешной сборки GitHub Actions:

```bash
sudo docker pull ghcr.io/spacesarmat/tgautoreply:latest
```

Затем пересоздайте контейнер. SQLite-база находится в bind mount и при обновлении образа не удаляется.
