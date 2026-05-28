[eaccchat] redirected to https://t.me/eaccchat — channel may not support public scraping (group chat or private). Consider removing it from the channel list.
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
{'read_mode': 'extract', 'per_stage_tokens': {'triage': {'prompt_tokens': 3650, 'completion_tokens': 708, 'calls': 1}, 'ad_filter': {'prompt_tokens': 10791, 'completion_tokens': 2688, 'calls': 13}, 'digest': {'prompt_tokens': 22149, 'completion_tokens': 1128, 'calls': 1}}, 'extraction_attempted': 34, 'extraction_ok': 29, 'urls_skipped_dedup': 0, 'urls_skipped_cap': 21}

===== DIGEST (HTML) =====
<a href="https://t.me/naebnet/15845">naebnet</a> [28.05.2026]
— Claude Opus 4.8 набирает 69.2% на SWE-Bench Pro и 57.9% на Humanity&#x27;s Last Exam
— В Claude Code выкатили режим dynamic workflows — агент может на ходу менять план действий при смене контекста
— Модель лучше работает над долгими задачами и чаще видит собственные ошибки

<a href="https://t.me/seeallochnaya/3660">seeallochnaya</a> [28.05.2026]
— Fast режим Opus 4.8 ускоряет генерацию в 2.5x и теперь стоит в 3 раза дешевле, чем для предыдущих моделей (было в 6 раз дороже базового, стало в 2)
— Добавлена гранулярная разбивка длины рассуждений, как у ChatGPT o-серии
— Анонсирован новый класс моделей с более высоким интеллектом, чем у Opus — релиз «в ближайшие недели» (Mythos)

<a href="https://t.me/cryptoEssay/3012">cryptoEssay</a> [28.05.2026]
— Dynamic workflows в Claude Code позволяют строить мультишаговые мультиагентные цепочки как единый промт: «прочитай A и B, сделай X и Y, проверь Z, сохрани в 1 и 2»
— Для автора это 90% рабочих задач — 30-минутные задачи по финансовому анализу, дашбордам, имейлам и проверке договоров модель теперь решает без участия человека

<a href="https://t.me/hacker_news_feed/128466">hacker_news_feed</a> [28.05.2026]
— На HN вышла игра «Continue? Y/N» — 60-секундный симулятор permission fatigue AI-агента (Score: 150+)
💡 Permission fatigue — это усталость от бесконечных подтверждений действий агента, когда человек начинает жать «да» не читая; игра высмеивает именно этот паттерн.

===== PERSONAL =====
<b>Лично тебе:</b>
• Dynamic workflows в Claude Code — прямой апгрейд для твоих LangGraph-пайплайнов: если сейчас ты вручную разбиваешь задачу на шаги и передаёшь контекст между агентами, Opus 4.8 позволяет формулировать весь сценарий одним структурированным промтом и доверить перепланирование модели.
• Fast режим Opus 4.8 стал в 3 раза дешевле при той же скорости 2.5x — для задач, где тебе нужен быстрый черновик или первичный скрининг (например, аналог Sourcer-агента из прошлого дайджеста), это уже экономически интересный выбор без компромисса по контексту.
• Игра про permission fatigue на HN перекликается с данными из предыдущего дайджеста (93% auto-approve в human-in-the-loop) — сигнал, что тема trust windows и адаптивной автономии созревает до уровня, где её стоит закладывать в control-plane своих агентных систем с самого начала.
