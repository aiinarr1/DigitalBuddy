# Digital Buddy — KMG Onboarding Demo v9 RAG

Эта версия основана на UI v8 и добавляет настоящий RAG-контур для Digital Buddy:

- загруженные ВНД PDF лежат в `backend/data/vnd_docs`;
- при запуске создаётся ChromaDB collection `kmg_vnd_documents`;
- документы и проверенные OCR-фрагменты индексируются как chunks;
- для каждого chunk создаётся embedding;
- вопрос сотрудника ищется через ChromaDB vector search;
- ответ возвращает документ, код документа, раздел, пункт, страницу и ссылку на PDF.

`rules_knowledge.json` больше не является основным механизмом ответа. Он используется как проверенный OCR seed для сканированных страниц, где PDF не имеет текстового слоя. Ответы бота проходят через ChromaDB.

## Демо-доступ

```text
Логин: aliya
Пароль: demo123
```

## Запуск

```bash
cp .env.example .env
docker compose down -v
docker compose up --build
```

Открыть:

```text
http://localhost:8000
```

ChromaDB доступен отдельно на:

```text
http://localhost:8001
```

## Проверка RAG

### Проверить статус индекса

```bash
curl http://localhost:8000/api/rag/status
```

В ответе должно быть:

```json
{
  "rag_engine": "ChromaDB",
  "collection": "kmg_vnd_documents",
  "chroma": {
    "ok": true,
    "count": 19
  }
}
```

Количество chunks может быть больше 19, если PDF имеет извлекаемый текстовый слой. Для сканированных PDF используются проверенные OCR seed-фрагменты.

### Принудительно пересобрать индекс

```bash
curl -X POST http://localhost:8000/api/rag/reindex
```

### Задать вопрос через RAG

```bash
curl -X POST http://localhost:8000/api/rag/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"Можно ли принять подарок от подрядчика?","bitrix_user_id":1001}'
```

Или через Bitrix webhook:

```bash
curl -X POST http://localhost:8000/webhooks/bitrix/bot-message \
  -H 'Content-Type: application/json' \
  -d '{"bitrix_user_id":1001,"message":"Можно ли принять подарок от подрядчика?"}'
```

Ответ содержит:

- естественный ответ Digital Buddy;
- `sources`;
- `rag_engine: ChromaDB`;
- `document_code`;
- `section`;
- `point`;
- `page`;
- `document_url`.

## Какие документы индексируются

Файлы находятся в:

```text
backend/data/vnd_docs/
```

Там лежат 4 ВНД:

1. `Кодекс_деловой_этики_АО.pdf`
2. `Инструкция_по_противодействию_коррупции.pdf`
3. `Правила_организации_пропускного.pdf`
4. `Должностная_инструкция_начальника_отдела_найма.pdf`

## Почему всё ещё есть rules_knowledge.json

Некоторые PDF являются сканами без текстового слоя. Без OCR такие страницы невозможно извлечь обычным PDF parser. Поэтому `rules_knowledge.json` используется как verified OCR seed: это проверенные фрагменты из этих же ВНД с точными разделами и пунктами.

Важно для защиты:

> Это не словарь ответов. Digital Buddy не выбирает ответ напрямую из JSON. При старте проекта chunks из PDF и verified OCR seed индексируются в ChromaDB, для них строятся embeddings, а вопрос сотрудника проходит через vector search. JSON нужен только как OCR-слой для сканированных страниц.

## Что говорить судьям

> В ранней версии правила лежали в JSON. Мы исправили это: теперь используется RAG-контур. ВНД PDF лежат в проекте, при старте они индексируются в ChromaDB. Для каждого фрагмента создаётся embedding. Когда сотрудник задаёт вопрос, Digital Buddy делает vector search по ChromaDB и возвращает ответ со ссылкой на конкретный документ, раздел, пункт и страницу. Для сканированных PDF мы добавили verified OCR chunks, потому что без OCR у них нет текстового слоя.

## Основной demo-flow

1. Войти как `aliya / demo123`.
2. Показать День 1: popup, видео, задачи.
3. Показать карточку Culture Fit на День 2.
4. Открыть AI Chat.
5. Спросить: `Можно ли принять подарок от подрядчика?`
6. Показать, что ответ содержит документ, раздел, пункт и ссылку.
7. Открыть `/api/rag/status` и показать ChromaDB collection.
8. При необходимости открыть `/api/documents/Инструкция_по_противодействию_коррупции.pdf`.

## Переменные окружения

```env
CHROMA_HOST=chromadb
CHROMA_PORT=8000
CHROMA_COLLECTION_NAME=kmg_vnd_documents
VND_DOCS_DIR=/app/seed_data/vnd_docs
RAG_TOP_K=5
RAG_EMBEDDING_DIM=384
FORCE_RAG_REINDEX=false
```

## Если ВНД поменялись

1. Замените PDF в `backend/data/vnd_docs`.
2. Если PDF сканированный и нет текстового слоя, обновите OCR-фрагменты в `backend/app/rules_knowledge.json`.
3. Пересоберите контейнер:

```bash
docker compose down -v
docker compose up --build
```

4. Или вызовите:

```bash
curl -X POST http://localhost:8000/api/rag/reindex
```

## RAG accuracy fix: no random answers

Version v10 adds guardrails for Digital Buddy RAG answers:

- high-confidence routing for working-time / early-leave / absence questions;
- query expansion for Russian/Kazakh synonyms before ChromaDB search;
- lexical relevance check in addition to ChromaDB vector distance;
- guarded fallback: if no relevant VND chunk is found, the bot says it did not find a precise point instead of returning a random rule;
- a new indexed chunk for working time and planned absence based on Culture Fit cards / PVT reference.

Quick test:

```bash
curl -X POST http://localhost:8000/api/rag/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"Можно ли уйти с работы пораньше?","bitrix_user_id":1001}'
```

Expected: answer says that early leaving / absence during work time must be agreed with the direct manager in advance, and refers to work-time / planned-absence rule.

## RAG coverage v11

Digital Buddy now uses a broader guarded RAG layer for employee questions. It still indexes the uploaded VND documents in ChromaDB, but before returning an answer it checks whether the question is grounded in one of the uploaded sources.

Supported with document/section/point sources:

- company values and ethics principles from the Code of Business Ethics;
- ethics, discrimination/harassment, confidentiality and reporting channels;
- anti-corruption, gifts, conflict of interest, bribery and reporting procedure;
- access control, proxy cards, visitor passes, after-hours and forbidden items;
- onboarding route, Day 1 tasks, Culture Fit and Digital Buddy functions;
- IT/workplace preboarding items from the onboarding assignment;
- HR responsibility for adaptation from the HR job instruction;
- SMART goals, 1:1 meetings and adaptation progress;
- emergency access mode from access-control rules.

Known employee topics that are not covered by the current uploaded VND return a safe fallback instead of a random answer, for example salary, vacation request procedure, VPN, Wi-Fi, payroll, benefits, full company org chart, CEO, corporate products/services, full IT helpdesk workflow, training catalog, procurement, booking rooms and career promotion process.

To rebuild the vector index after replacing documents:

```bash
curl -X POST http://localhost:8000/api/rag/reindex
```

To check RAG status:

```bash
curl http://localhost:8000/api/rag/status
```

## v11: Broad VND question coverage and safe fallback

Digital Buddy now supports a broader employee-question router for common VND and onboarding topics:

- company values and Code of Ethics;
- onboarding and adaptation route;
- access/pass regime;
- anti-corruption and gifts;
- confidentiality and personal data;
- HR adaptation responsibilities;
- emergency / access-control situations;
- work-time absence and lateness reminders.

For topics that are **not present in the loaded VND set** (salary, vacation balances, VPN, Wi-Fi, payroll, individual schedule, KPI, internal vacancies, etc.), the bot now returns a transparent fallback instead of a random answer. It explains that an exact point was not found and says which additional regulation/integration is needed.

Run reindex after changing VND files:

```bash
curl -X POST http://localhost:8000/api/rag/reindex
```

Recommended demo checks:

```bash
curl -X POST http://localhost:8000/api/rag/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"Какие ценности компании?","bitrix_user_id":1001}'

curl -X POST http://localhost:8000/api/rag/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"Кто является генеральным директором компании?","bitrix_user_id":1001}'

curl -X POST http://localhost:8000/api/rag/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"Можно ли передать свой пропуск коллеге?","bitrix_user_id":1001}'
```

## v12: исправление Digital Buddy RAG

В версии v12 чат-бот работает по функциям из таблицы Digital Buddy:

- приветствие и ориентация в День 1;
- ответы по ВНД 24/7 через ChromaDB + embeddings;
- Culture Fit Nudges;
- напоминания и подготовка к 1:1;
- помощь с целями SMART;
- анализ тональности и risk-сигналы для HR.

### Что изменено в RAG

1. PDF/DOCX документы регистрируются в SQLite в таблице `rag_documents`.
2. Индексированные фрагменты документов зеркалируются в SQLite в таблице `rag_indexed_chunks`.
3. ChromaDB остаётся основным vector-search слоем.
4. Если вопрос относится к покрытой теме, бот отвечает только из индексированных ВНД chunks.
5. Если в загруженных ВНД нет точного пункта, бот не придумывает ответ и отправляет сотрудника в HR/IT/руководителю.

### Проверка документов в базе

```bash
curl http://localhost:8000/api/rag/documents
```

### Проверка ChromaDB

```bash
curl http://localhost:8000/api/rag/status
```

### Пересборка индекса

```bash
curl -X POST http://localhost:8000/api/rag/reindex
```

### Тестовые вопросы

```text
Какие функции у чат-бота?
Можно ли уйти с работы пораньше?
Можно ли принять подарок от подрядчика?
Можно ли передать пропуск коллеге?
Что делать при чрезвычайной ситуации?
Кто отвечает за адаптацию новых сотрудников?
Как посмотреть расчетный лист?
```

Последний вопрос должен вернуть safe fallback, потому что в загруженных ВНД нет точного регламента по расчетному листу.
