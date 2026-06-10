# Demo Script — Digital Buddy RAG v9

## 1. Start

```bash
cp .env.example .env
docker compose down -v
docker compose up --build
```

Open:

```text
http://localhost:8000
```

Login:

```text
aliya / demo123
```

## 2. Show mandatory scenarios

### Scenario 1 — Day 1

- Login as employee.
- Click Digital Buddy icon.
- Show fullscreen popup:
  - greeting;
  - Chairman video;
  - Day 1 tasks;
  - progress.
- Open a task and show task details.

### Scenario 2 — Culture Fit Nudge

- Click presentation control / update status to next day.
- Show Culture Fit card.
- Show duplicate message in chat.
- Repeat login and show that duplicate card does not appear.

### Scenario 3 — RAG answer from VND

Open AI Chat and ask:

```text
Можно ли принять подарок от подрядчика?
```

Expected answer must contain:

- document name;
- document code;
- section;
- point;
- page;
- PDF link.

## 3. Show that it is real RAG

Open:

```text
http://localhost:8000/api/rag/status
```

Say:

> Здесь видно, что используется ChromaDB collection `kmg_vnd_documents`. Документы ВНД лежат в `backend/data/vnd_docs`, chunks индексируются, а вопросы проходят через vector search.

Then run:

```bash
curl -X POST http://localhost:8000/api/rag/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"Можно ли передать свой пропуск коллеге?","bitrix_user_id":1001}'
```

Expected source:

```text
Правила организации пропускного и внутриобъектового режимов
KMG-PR-1186.5-22
Раздел 5.8
п. 5.8.20
```

## 4. What to say if judges ask “is it JSON?”

Say:

> JSON используется только как verified OCR layer для сканированных PDF-страниц. Основной поиск идёт через ChromaDB: документы разбиваются на chunks, для chunks создаются embeddings, вопрос сотрудника превращается в embedding, дальше ChromaDB возвращает top-k релевантных фрагментов. Ответ Digital Buddy строится из найденного фрагмента и всегда содержит ссылку на ВНД, раздел, пункт и страницу.

## 5. Useful test questions

```text
Можно ли принять подарок от подрядчика?
Что делать, если мне предлагают взятку?
Куда сообщить о коррупции?
Можно ли передать свой пропуск коллеге?
Что делать с кабинетом после окончания рабочего дня?
Кто отвечает за адаптацию новых сотрудников?
Маған сыйлық ұсынса не істеу керек?
```

## Extra RAG QA test for judges

Question:

```text
Можно ли уйти с работы пораньше?
```

Expected Digital Buddy behavior:

- no random answer;
- answer refers to working time / planned absence;
- says to agree with the direct manager in advance;
- if formal absence is needed, clarify with manager or HR;
- includes source and point.

## v12 RAG demo script

1. Откройте чат Digital Buddy и спросите: `Какие функции у чат-бота?`
   - Бот должен перечислить 6 функций: День 1, RAG 24/7, Culture Fit, 1:1, SMART, анализ.

2. Спросите: `Можно ли принять подарок от подрядчика?`
   - Бот должен ответить по Инструкции по противодействию коррупции, указать документ, раздел и пункт.

3. Спросите: `Можно ли передать пропуск коллеге?`
   - Бот должен ответить по Правилам пропускного режима.

4. Спросите: `Кто отвечает за адаптацию новых сотрудников?`
   - Бот должен ответить по должностной инструкции HR-блока.

5. Спросите: `Как посмотреть расчетный лист?`
   - Бот должен честно сказать, что в загруженных ВНД точного пункта нет, и не давать случайный ответ.

Фраза для судей:

> В v12 PDF/DOCX документы зарегистрированы в базе, chunks зеркалируются в SQLite и индексируются в ChromaDB. Digital Buddy отвечает только при наличии источника: документ, раздел, пункт и ссылка. Если точного пункта нет, бот не галлюцинирует.
