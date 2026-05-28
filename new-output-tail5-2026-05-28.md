[eaccchat] redirected to https://t.me/eaccchat — channel may not support public scraping (group chat or private). Consider removing it from the channel list.
Ad-filter batch failed, keeping all: 1 validation error for AdBatchResult
posts
  Field required [type=missing, input_value={'$defs': {'PostAdLabel':...sult', 'type': 'object'}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.12/v/missing
Ad-filter batch failed, keeping all: 1 validation error for AdBatchResult
posts
  Field required [type=missing, input_value={'$defs': {'PostAdLabel':...sult', 'type': 'object'}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.12/v/missing
[try_reader] read_mode=extract channels=['cryptoEssay', 'llm_notes', 'ai_newz', 'y_everyday', 'eaccchat', 'eboutdatascience', 'seeallochnaya', 'bogdanisssimo', 'naebnet', 'hacker_news_feed']
  … 📡 Загружаю список каналов...
  … ⏳ Скрейплю каналы параллельно...
  … 📖 Читаю статьи по ссылкам...
  … 🔍 Фильтрую рекламу...
  … 🤖 Генерирую дайджест...
  … 💾 Сохраняю в базу...
[try_reader] append_to_history skipped (read-only test)

===== COST SUMMARY =====
{'read_mode': 'extract', 'per_stage_tokens': {'triage': {'prompt_tokens': 3650, 'completion_tokens': 539, 'calls': 1}, 'ad_filter': {'prompt_tokens': 10791, 'completion_tokens': 2801, 'calls': 13}, 'digest': {'prompt_tokens': 23466, 'completion_tokens': 1844, 'calls': 1}}, 'extraction_attempted': 34, 'extraction_ok': 32, 'urls_skipped_dedup': 0, 'urls_skipped_cap': 21}

===== DIGEST (HTML) =====
<a href="https://t.me/naebnet/15845">naebnet</a> [28.05.2026 17:20 UTC]
— Claude Opus 4.8: 69.2% на SWE-Bench Pro, 57.9% на Humanity&#x27;s Last Exam
— Dynamic workflows в Claude Code — агент перестраивает план на ходу при смене контекста
— Модель чаще обнаруживает собственные ошибки на длинных задачах

<a href="https://t.me/seeallochnaya/3660">seeallochnaya</a> [28.05.2026 16:53 UTC]
— Fast режим Opus 4.8: было в 6 раз дороже базового, стало в 2 раза дороже — итого в 3 раза дешевле предыдущей модели
— Добавлена гранулярная разбивка длины рассуждений (как у ChatGPT o-серии) — пользователь сам управляет effort
— Анонсирован новый класс моделей с интеллектом выше Opus — «в ближайшие недели» (предположительно Mythos)

<a href="https://t.me/cryptoEssay/3012">cryptoEssay</a> [28.05.2026 17:59 UTC]
— Dynamic workflows — основная фича релиза для практического использования: позволяет описывать задачу как многошаговый скрипт («прочитай A и B, сделай C, проверь X, сохрани в Y»)
— Автор уверен, что 30-минутную задачу (финансовый анализ + дашборд + имейл + проверка договора) Opus 4.8 закроет автономно

<a href="https://t.me/bogdanisssimo/3972">bogdanisssimo</a> [28.05.2026 01:12 UTC]
— $100/мес план Anthropic эквивалентен ~$1,200 токенов в Claude Code при умеренном использовании — 12x value на подписке vs API
— Корпоративные клиенты платят по API-ценам без discount&#x27;ов, которые есть у физлиц на подписке
— Anthropic близок к первому прибыльному кварталу — компании удивляются размеру LLM-счетов от корпоративного использования

<a href="https://t.me/ai_newz/4594">ai_newz</a> [28.05.2026 15:29 UTC]
— Whitepaper Сбера AI-Disrupt PDLC (337 тыс. знаков): целевая аудитория C-level, но внутри есть архитектурный слой для инженеров
— Тезис: код становится вторичным артефактом, первична спецификация (намерение)
— Сбер сейчас на 3-м уровне зрелости из 5 по собственной шкале

<a href="https://t.me/hacker_news_feed/128466">hacker_news_feed</a> [28.05.2026 19:40 UTC]
— «Continue? Y/N» — 60-секундная игра на HN о permission fatigue AI-агентов: симулирует поток human-in-the-loop запросов
— Артефакт культуры: проблема автоматического аппрува HITL-запросов дошла до формата show HN
💡 Permission fatigue — когда агент так часто просит подтверждения, что пользователь начинает жать «да» не читая; игра буквально это и воспроизводит.

<a href="https://t.me/hacker_news_feed/128460">hacker_news_feed</a> [28.05.2026 13:40 UTC]
— Пять frontier LLM расходятся в ответах на 67% из 1000 реальных fact-check утверждений
— Консенсус между моделями не является надёжным индикатором фактической точности

<i>ещё: Anthropic ARR вышел на $47B (аннуализированный), оценка компании $900B — seeallochnaya/bogdanisssimo · Go proposal: поддержка generic methods — обсуждение реализации без мономорфизации — hacker_news_feed · Apple и Google превратили push-уведомления в активный intermediary layer: резюмируют, переупорядочивают, местами перепишут — hacker_news_feed</i>

===== PERSONAL =====
<b>Лично тебе:</b>
• Dynamic workflows в Claude Code — прямой апгрейд для твоего рабочего паттерна: если ты уже пишешь промпты как многошаговые скрипты (читай → сделай → проверь → сохрани), это нативная поддержка на уровне runtime, а не workaround через цепочку вызовов.
• Fast режим Opus 4.8 стал дешевле в 3 раза — при активном использовании в subagents и параллельных задачах это реальное снижение стоимости прогона; стоит пересчитать текущие cost estimates для пайплайнов где fast mode уже включён или планировался.
• Show HN с игрой про permission fatigue — это косвенное подтверждение тезиса из вчерашнего дайджеста про 93% автоматических аппрувов: проблема достаточно реальная, чтобы стать культурным мемом. Если в твоих агентных системах есть HITL — стоит проревьюить, где он реально нужен, а где это просто шум.
