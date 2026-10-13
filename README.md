# tg-mcp

[![ci](https://github.com/andrewkomkov/tg-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/andrewkomkov/tg-mcp/actions/workflows/ci.yml)
[![tdlib-watch](https://github.com/andrewkomkov/tg-mcp/actions/workflows/tdlib-watch.yml/badge.svg)](https://github.com/andrewkomkov/tg-mcp/actions/workflows/tdlib-watch.yml)

Telegram в терминале и как MCP-сервер. Одна команда `tg`: CLI-клиент и
`tg mcp` для Claude Code и других MCP-клиентов. Работает от имени вашего
аккаунта, а не бота.

## Как это устроено

`tg-mcp` — не форк клиента Telegram. Он использует
[TDLib](https://core.telegram.org/tdlib), официальную библиотеку Telegram, как
зависимость. TDLib приходит готовым колесом
[`tdjson`](https://pypi.org/project/tdjson/) для Linux и macOS (arm64).
Сборку `tdjson` ведёт сторонний автор из исходников TDLib.

Код обращается к TDLib через JSON-интерфейс и не содержит сгенерированных
типов. При запуске `tg` скачивает схему API для установленного коммита TDLib и
проверяет по ней каждый запрос до отправки. Если поле переименовали, вы
получаете ошибку с названием поля и списком допустимых полей.

## Установка

```sh
git clone https://github.com/andrewkomkov/tg-mcp && cd tg-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
```

Или как отдельный инструмент: `pipx install git+https://github.com/andrewkomkov/tg-mcp`.

Нужен Python 3.10 или новее. Windows не поддерживается.

## Вход

1. Создайте `api_id` и `api_hash` на <https://my.telegram.org/apps>. Создать их
   нужно один раз. Telegram требует собственную пару для каждого приложения.
2. Запустите `tg login`. Команда спросит `api_id`, `api_hash`, номер телефона,
   код и пароль двухфакторной защиты. `tg login --qr` входит по QR-коду.

Без терминала вход идёт по шагам: `tg login --phone ...`, затем
`tg login --code ...`, затем `tg login --password ...`. В MCP те же шаги
выполняет инструмент `tg_login`.

Данные лежат в `~/.tg-mcp/` (переменная `TG_MCP_HOME` меняет каталог):
`config.json` с ключами API и `profiles/<имя>/` с базой TDLib. Пару ключей можно
задать переменными `TG_API_ID` и `TG_API_HASH` или записать в файл `.env` в
корне проекта либо в текущем каталоге. Переменные окружения важнее файла.

## CLI

```sh
tg chats --unread                       # чаты с непрочитанным
tg chats --folder Работа                # чаты из папки
tg read "Название чата" -n 50           # последние сообщения
tg read @channel --since -7d -n 200     # сообщения за неделю
tg send @username привет                # отправить
echo "длинный текст" | tg send me
tg send me -f a.jpg -f b.jpg "альбом"   # несколько файлов
tg send me "позже" --schedule +2h       # отложенная отправка
tg search "договор" -c @username --filter document
tg watch                                # новые сообщения в реальном времени
```

Команды по группам:

| Группа | Команды |
| --- | --- |
| Чтение | `chats`, `folders`, `chat`, `read`, `message`, `search`, `topics`, `watch`, `download` |
| Сообщения | `send`, `poll`, `edit`, `delete`, `forward`, `react`, `pin`, `draft`, `click` |
| Чаты | `chat-action`, `mark-read`, `join`, `create`, `edit-chat`, `invite-link` |
| Люди | `members`, `member`, `contacts`, `contact` |
| Аккаунт | `login`, `logout`, `me`, `profile`, `sessions`, `status` |
| TDLib | `api search`, `api show`, `call`, `selfcheck` |
| Сервер | `mcp`, `serve` |

`tg <команда> --help` показывает параметры. Флаг `--json` перед командой
печатает JSON: `tg --json chats`.

Чат задаётся идентификатором, `@username`, ссылкой `t.me/...`, номером телефона,
словом `me` или названием. Если название подходит нескольким чатам, команда
останавливается и печатает их идентификаторы.

Время задаётся датой (`2026-01-31`), датой и временем (`2026-01-31T18:00`) или
смещением (`+2h`, `-7d`).

Несколько аккаунтов: `tg -p work login`, затем `tg -p work chats`.

## MCP

```sh
claude mcp add -s user telegram -- /полный/путь/к/.venv/bin/tg mcp
```

Сервер даёт 35 инструментов с теми же операциями, что CLI. `tg mcp --read-only`
оставляет только чтение и `tg_login`.

## Полный API TDLib

Отдельные команды закрывают частые действия. Остальное доступно через прямой
вызов TDLib:

```sh
tg api search "sticker set"             # найти метод по словам
tg api show getStickerSet               # поля и описание
tg call getStickerSet '{"set_id": "1"}' # вызвать
```

В MCP это инструменты `tg_api_search`, `tg_api_describe` и `tg_call`. Методы,
которые удаляют аккаунт или завершают сессии, для прямого вызова закрыты. Флаг
`tg call --unsafe` снимает запрет.

## Ограничения

- Звонки. TDLib передаёт только сигналы вызова и не содержит аудио- и видеостека.
- Секретные чаты отключены.
- Мини-приложения и оплата. Для них нужен встроенный браузер.

## CLI и MCP одновременно

TDLib разрешает одной базе один процесс. Поэтому первый процесс `tg` берёт
сессию и открывает unix-сокет, а остальные работают через него. Пока запущен
`tg mcp`, команды CLI отвечают сразу и идут через тот же процесс. `tg serve`
держит сессию без MCP.

## Обновление TDLib

```sh
.venv/bin/pip install -U tdjson
.venv/bin/tg selfcheck
```

`tg selfcheck` сверяет все запросы в коде со схемой установленной TDLib.

В репозитории это происходит автоматически. Задача `tdlib-watch` каждые три
часа ставит последнюю версию `tdjson`. Если версия новая, задача запускает
тесты и сверку со схемой. При успехе она записывает версию в файл
`TDLIB_VERIFIED`. При ошибке она создаёт issue со списком расхождений.

Тесты в CI работают без аккаунта Telegram. Они проверяют схему, форматы и запуск
MCP-сервера. Работу с настоящими чатами CI не проверяет.

## Проверка

```sh
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

## Лицензия

MIT.
