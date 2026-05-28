[eaccchat] redirected to https://t.me/eaccchat — channel may not support public scraping (group chat or private). Consider removing it from the channel list.
[try_reader] read_mode=extract channels=['cryptoEssay', 'llm_notes', 'ai_newz', 'y_everyday', 'eaccchat', 'eboutdatascience', 'seeallochnaya', 'bogdanisssimo', 'naebnet', 'hacker_news_feed']
  … 📡 Загружаю список каналов...
  … ⏳ Скрейплю каналы параллельно...
  … 📖 Читаю статьи по ссылкам...
  … 🔍 Фильтрую рекламу...
  … 🤖 Генерирую дайджест...
  … 💾 Сохраняю в базу...
[try_reader] append_to_history skipped (read-only test)

===== COST SUMMARY =====
{'read_mode': 'extract', 'per_stage_tokens': {'triage': {'prompt_tokens': 3650, 'completion_tokens': 539, 'calls': 1}, 'ad_filter': {'prompt_tokens': 10791, 'completion_tokens': 2929, 'calls': 13}, 'digest': {'prompt_tokens': 22915, 'completion_tokens': 1972, 'calls': 1}}, 'extraction_attempted': 34, 'extraction_ok': 29, 'urls_skipped_dedup': 0, 'urls_skipped_cap': 21}

===== DIGEST (HTML) =====
<a href="https://t.me/naebnet/15845">naebnet</a> [28.05.2026 17:20 UTC]
— Claude Opus 4.8 набирает 69,2% на SWE-Bench Pro и 57,9% на Humanity&#x27;s Last Exam
— Dynamic workflows в Claude Code: агент меняет план действий на ходу при смене контекста
— Модель лучше работает на длинных задачах и чаще обнаруживает собственные ошибки

<a href="https://t.me/seeallochnaya/3660">seeallochnaya</a> [28.05.2026 16:53 UTC]
— Fast режим Opus 4.8 теперь в 3 раза дешевле прошлых моделей: было в 6× дороже базового, стало в 2×
— Гранулярная разбивка длины рассуждений — как у ChatGPT o-серии, пользователь управляет глубиной
— Анонс нового класса моделей выше Opus по интеллекту — «в ближайшие недели» (предположительно Mythos)

<a href="https://t.me/ai_newz/4595">ai_newz</a> [28.05.2026 17:29 UTC]
— Лимиты токенов в Claude Code увеличены синхронно с ростом потребления на более высоких уровнях усилий
— Модель декларируется более честной: реже срезает углы, чаще признаёт незнание

<a href="https://t.me/cryptoEssay/3012">cryptoEssay</a> [28.05.2026 17:59 UTC]
— Dynamic workflows — 90% рабочего использования Claude: один промпт типа «прочитай А и Б, сделай В, проверь Г, сохрани в Д, протестируй» без ручного сопровождения
— Задача на 30 минут (финансовый анализ + дашборд + имейл + проверка договора) выполняется агентом целиком самостоятельно

<a href="https://t.me/hacker_news_feed/128466">hacker_news_feed</a> [28.05.2026 19:40 UTC]
— Show HN: «Continue? Y/N» — 60-секундная игра про permission fatigue AI-агентов
— Игра моделирует поток запросов на подтверждение от агента и усталость от него — прямая отсылка к проблеме 93% автоматических аппрувов из whitepaper Сбера
💡 Permission fatigue — когда человек устаёт нажимать «разрешить» на каждый шаг агента и начинает аппрувить не глядя.

<a href="https://t.me/bogdanisssimo/3972">bogdanisssimo</a> [28.05.2026 01:12 UTC]
— Тяжёлый пользователь Claude Code + OpenAI Codex на $200/мес (Max+Pro планы) потребил бы токенов на $2 180 по API-ценам за 30 дней
— Энтерпрайз-клиенты платят полные API-цены без скидок, которые есть у подписчиков — отсюда неожиданно высокие счета
— Anthropic близка к первому прибыльному кварталу по слухам; ARR вышел на $47 млрд (по последнему месяцу)

<a href="https://t.me/hacker_news_feed/128464">hacker_news_feed</a> [28.05.2026 17:10 UTC]
— Claude Opus 4.8 на HN: Score 192+ за 20 минут — один из самых быстрых наборов очков за день

<i>ещё: Anthropic ARR $47 млрд, оценка $900 млрд, раунд $65 млрд — seeallochnaya/bogdanisssimo · Go proposal: generic methods (type parameters на конкретных методах) — hacker_news_feed · Apple и Google как активные посредники в push-уведомлениях: суммаризация и перезапись на устройстве — hacker_news_feed · YouTube начал блокировать VPN для региональных лицензий (спортивные трансляции, ТВ-премьеры) — naebnet · Wildberries запустил кнопку «Предложить цену» в разделе WB Ресейл — naebnet · Число отменённых авиарейсов в России выросло в 4 раза за янв–май 2026 — naebnet · AMD изменила лицензирование Vivado для Linux-пользователей без предупреждения — hacker_news_feed · Сотрудник Google обвиняется в инсайдерской ставке на Polymarket на $1 млн по поисковым трендам — hacker_news_feed</i>

===== PERSONAL =====
<b>Лично тебе:</b>
• Dynamic workflows в Claude Code — прямой апгрейд к твоему стеку: если ты уже строишь мультишаговые агентные пайплайны через LangGraph/deepagents, стоит проверить, насколько нативный dynamic workflows в CC перекрывает кастомную логику переплнирования — может убрать слой.
• Fast режим Opus 4.8 подешевел с 6× до 2× от базового при 2.5× скорости — для subagents и параллельных веток в агентном графе это меняет экономику: раньше fast был нерентабелен для фоновых задач, теперь считается.
• Игра «Continue? Y/N» и цифра 93% авто-аппрувов — не просто статистика: если ты строишь control-plane для агентов, это сигнал что human-in-the-loop как архитектурный примитив сломан по умолчанию и нужны trust windows или пакетные одобрения прямо в дизайне.

[try_reader] sent digest to chat 468775848
