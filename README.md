# Digest Bot

Персональный Telegram-бот: ежедневный дайджест из публичных каналов с AI-персонализацией.

---

## Запуск на Windows (локально)

### 1. Установи uv (если нет)
```
winget install astral-sh.uv
```
или с сайта: https://docs.astral.sh/uv/getting-started/installation/

### 2. Заполни .env
```
BOT_TOKEN=...       # от @BotFather
CHAT_ID=...         # твой ID от @userinfobot
OPENROUTER_KEY=...  # от openrouter.ai
```

### 3. Установи зависимости и запусти
```bash
cd digest_bot
uv sync
uv run bot.py
```

Или просто дважды кликни **run.bat**.

### 4. В Telegram отправь /start своему боту

---

## Запуск в Docker (на сервере)

### Что нужно на сервере
- Docker + Docker Compose (v2)
- `.env` файл с ключами (тот же что на Windows)
- Интернет для доступа к api.telegram.org и openrouter.ai

### Порты
Бот работает через **polling** — входящих портов не нужно.
Не конфликтует с другими ботами на сервере.

### Деплой

```bash
# 1. Скопируй папку на сервер (scp, rsync или git)
scp -r digest_bot/ user@server:/opt/digest_bot/

# 2. Зайди на сервер
ssh user@server

# 3. Перейди в папку
cd /opt/digest_bot

# 4. Убедись что .env заполнен
cat .env

# 5. Запусти
docker compose up -d

# Логи
docker compose logs -f

# Остановить
docker compose down

# Перезапустить после обновления кода
docker compose up -d --build
```
 
### Персистентность данных
`data.json` и `digests_history.json` монтируются как volumes — данные сохраняются между перезапусками контейнера.

Если файлов ещё нет — Docker создаст их автоматически при первом запуске.

---

## Функции бота

| Кнопка | Что делает |
|--------|-----------|
| 📰 Дайджест | Собрать и прислать дайджест прямо сейчас |
| 📚 История | Листать предыдущие дайджесты |
| 👤 Профиль | Посмотреть и изменить свой профиль |
| ⚙️ Настройки | Выбрать AI-модель, управлять каналами, авто-сброс фокуса |
| 🎯 Задать фокус | Указать тему для следующего дайджеста |

**Расписание:** 13:00 МСК — дайджест, 18:00 МСК — чекин.

---

## Автодеплой через GitHub Actions

При каждом `git push` в ветку `main` Actions SSH-ит на сервер и перезапускает Docker.

### Настройка (один раз)

**1. Добавь секреты в GitHub репозитории** (`Settings → Secrets → Actions`):

| Secret | Значение |
|--------|---------|
| `SERVER_HOST` | IP или домен сервера |
| `SERVER_USER` | Пользователь (например `root` или `ubuntu`) |
| `SERVER_SSH_KEY` | Приватный SSH-ключ (содержимое `~/.ssh/id_rsa`) |
| `SERVER_PORT` | SSH порт (обычно `22`) |
| `DEPLOY_PATH` | Путь к папке на сервере (например `/opt/digest_bot`) |

**2. На сервере:** убедись что публичный ключ добавлен в `~/.ssh/authorized_keys`, репозиторий склонирован в `DEPLOY_PATH`, `.env` заполнен.

**3. Первый запуск вручную:**
```bash
ssh user@server
cd /opt/digest_bot
git clone https://github.com/ВАШ_ЮЗЕР/digest_bot.git .
cp .env.example .env  # заполни ключи
docker compose up -d --build
```

После этого каждый `git push main` → деплой автоматически (~30 сек).

---

## Обновление кода

**Windows:**
```bash
# Остановить бот (Ctrl+C в терминале), затем:
uv sync
uv run bot.py
```

**Docker:**
```bash
docker compose up -d --build
```

---

## Проверка работоспособности

```bash
uv run test_smoke.py
```

Тест проверяет: скрапер, AI (генерация дайджеста со ссылками), фильтрацию картинок, отправку в Telegram.
