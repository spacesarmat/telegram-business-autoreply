# GitHub / GHCR setup

Target repository:

`spacesarmat/telegram-business-autoreply`

Container image:

`ghcr.io/spacesarmat/tgautoreply:latest`

## Required once

1. Repository `spacesarmat/telegram-business-autoreply` is the target repository.
2. Push this project to branch `main`.
3. Open **Actions** and verify **Publish GHCR image** succeeds.
4. Open the created Container package settings and set package visibility to **Public** so ZimaOS can pull it anonymously.

After that, every push to `main` rebuilds `latest` for `linux/amd64` and `linux/arm64`.

## GitHub Actions runtime

Начиная с v1.5.1 workflow использует Node 24-compatible major-версии официальных Actions:

- `actions/checkout@v5`
- `docker/setup-qemu-action@v4`
- `docker/setup-buildx-action@v4`
- `docker/login-action@v4`
- `docker/metadata-action@v6`
- `docker/build-push-action@v7`

Переменную `ACTIONS_ALLOW_USE_UNSECURE_NODE_VERSION` включать не требуется.


## v1.5.2 — timezone

Видимые даты и время используют IANA-часовой пояс из `TZ`; внутренние timestamps SQLite остаются UTC. Для Docker добавлена зависимость `tzdata`.

## v1.6.0 — pricing

В версии 1.6.0 добавлен автоматический расчёт аренды. Новых GitHub Secrets или переменных окружения не требуется. После обновления откройте `/admin → 💰 Тарифы`, настройте нужную форму и только затем включите расчёт.

## v1.7.0 — дополнительные услуги и буферы

Новых GitHub Secrets или переменных окружения не требуется. После обновления откройте `/admin → 💰 Тарифы`, выберите форму и настройте **🧰 Доп. услуги**, **🛠 Буфер до** и **🧹 Буфер после**. Все новые значения по умолчанию безопасны: буферы равны нулю, а список услуг пуст.
## v1.7.1 — гости и уведомления о цене

Новых GitHub Secrets или переменных окружения не требуется. После обновления существующие стандартные вопросы **«Количество гостей»** автоматически мигрируют на тип с диапазонами. При ручном изменении стоимости из карточки заявки бот использует сохранённый `business_connection_id` и уведомляет клиента в исходном Business-чате.


## Web Admin (v1.8.0)

Для включения веб-админки задайте в ZimaOS/CasaOS окружение:

```env
WEB_ADMIN_USERNAME=admin
WEB_ADMIN_PASSWORD=your-strong-password-here
```

После запуска откройте `http://IP_ZIMAOS:18080/admin`. Пароль должен быть не короче 10 символов. Заглушка `PASTE_STRONG_WEB_ADMIN_PASSWORD_HERE` намеренно отключает панель, пока вы не замените её. `/health` остаётся публичным для Docker healthcheck.

Не публикуйте порт 18080 напрямую в интернет без HTTPS/reverse proxy.

## v1.8.1 — выбор формата кнопками

Новых Secrets или переменных окружения не требуется. После обновления стандартный вопрос **«Формат»** в форме **«Аренда Фабрики»** автоматически станет типом `🎛 Выбор из вариантов` и получит стартовый список кнопок. Варианты можно менять через Telegram `/admin` или Web Admin.

## v1.8.2 — concurrency safety

В Docker/ZimaOS можно дополнительно задать:

```env
MAX_CONCURRENT_UPDATES=32
```

Это максимальное число Telegram updates, которые выполняются одновременно. События одного чата всегда сериализуются независимо от этого значения. После обновления SQLite автоматически добавит `submission_token` для активных форм и UNIQUE-индекс для новых заявок.
