# Как подключить данные

Для аудита и черновой структуры ключи не нужны. Всё ниже необязательно и подключается по отдельности. Интерфейсы сервисов меняются, поэтому названия кнопок могут отличаться.

Ключи и токены остаются на вашем компьютере. Не отправляйте их в чат с агентом и не добавляйте в git.

## Yandex Search API и Вордстат

Дают живую выдачу, спрос по региону, группы запросов по топу и оценку шансов. Услуга платная.

1. Создайте платёжный аккаунт и каталог в [Yandex Cloud](https://console.yandex.cloud/).
2. Создайте сервисный аккаунт и выдайте ему в каталоге роль `search-api.webSearch.user`.
3. Создайте для него API-ключ.
4. Скопируйте `.env.example` в `.env` и заполните `YANDEX_SEARCH_API_KEY` и `YANDEX_FOLDER_ID`.

Проверьте план и стоимость, ничего не отправляя:

```bash
seo serp-submit
```

## Яндекс Вебмастер

Показывает запросы и страницы своего сайта, индексацию и диагностику. Сайт должен быть подтверждён в Вебмастере.

1. Создайте приложение на [oauth.yandex.ru](https://oauth.yandex.ru/client/new).
2. Добавьте Redirect URI `https://oauth.yandex.ru/verification_code`.
3. Выдайте приложению доступ к данным Вебмастера.
4. Скопируйте ClientID и выполните:

```bash
seo auth yandex --client-id <ClientID>
```
```bash
seo search-collect --source yandex
```

Токен вводится в скрытое поле терминала и действует 6 месяцев.

## Яндекс Метрика

Показывает визиты и конверсии. Сервис запрашивает доступ только на чтение (`metrika:read`), поэтому приложению нужно это право. Можно использовать то же приложение, добавив право, или отдельное.

```bash
seo auth metrika --client-id <ClientID>
```
```bash
seo metrika-collect --days 90
```

## Google Search Console

1. В [Google Cloud Console](https://console.cloud.google.com/) создайте проект и включите Google Search Console API.
2. Настройте экран согласия OAuth и добавьте свой аккаунт в тестовые пользователи.
3. Создайте идентификатор OAuth типа «Приложение для компьютера» и скачайте JSON в `data/google-client.json`.
4. Выполните:

```bash
seo auth google --client-file data/google-client.json
```
```bash
seo search-collect --source google --days 480
```

В тестовом режиме приложения вход действует 7 дней. Сервис запрашивает только чтение (`webmasters.readonly`).
