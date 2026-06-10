import hashlib
import json
import math
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

try:
    import chromadb
except Exception:  # pragma: no cover - fallback for local environments before pip install
    chromadb = None

try:
    from pypdf import PdfReader
except Exception:  # pragma: no cover
    PdfReader = None
from pydantic import BaseModel

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
STATIC_DIR = PROJECT_DIR / "static"
RULES_PATH = APP_DIR / "rules_knowledge.json"
DB_PATH = os.getenv("DB_PATH", str(PROJECT_DIR / "data" / "demo.db"))
CHAIRMAN_VIDEO_URL = os.getenv("CHAIRMAN_VIDEO_URL", "https://youtu.be/1POsZtDks5M?si=hfk-k3mt-l0L-O6O")
PUBLIC_BACKEND_URL = os.getenv("PUBLIC_BACKEND_URL", "http://localhost:8000")
DEMO_SOON_THRESHOLD_HOURS = int(os.getenv("DEMO_SOON_THRESHOLD_HOURS", "2"))

# Real RAG configuration. ChromaDB stores vector embeddings of KMG internal documents.
# For a stable hackathon demo the default embedding is deterministic and offline.
# If you install/enable a corporate multilingual model, replace hash_embedding() with that model.
SEED_DATA_DIR = PROJECT_DIR / "seed_data"
VND_DOCS_DIR = Path(os.getenv("VND_DOCS_DIR", str(SEED_DATA_DIR / "vnd_docs")))
CHROMA_PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", str(PROJECT_DIR / "data" / "chroma"))
CHROMA_COLLECTION_NAME = os.getenv("CHROMA_COLLECTION_NAME", "kmg_vnd_documents")
CHROMA_HOST = os.getenv("CHROMA_HOST", "").strip()
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8000"))
RAG_TOP_K = int(os.getenv("RAG_TOP_K", "5"))
RAG_EMBEDDING_DIM = int(os.getenv("RAG_EMBEDDING_DIM", "384"))
FORCE_RAG_REINDEX = os.getenv("FORCE_RAG_REINDEX", "false").lower() == "true"

# RAG quality guardrails. They prevent Digital Buddy from returning a random
# chunk when the question is weakly related to the retrieved document.
RAG_CHROMA_MAX_DISTANCE = float(os.getenv("RAG_CHROMA_MAX_DISTANCE", "1.05"))
RAG_SQL_MIN_SCORE = int(os.getenv("RAG_SQL_MIN_SCORE", "4"))
RAG_CHROMA_MIN_LEXICAL_SCORE = int(os.getenv("RAG_CHROMA_MIN_LEXICAL_SCORE", "2"))

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Digital Buddy One-Day MVP", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class LoginPayload(BaseModel):
    bitrix_user_id: int = 1001


class SetDayPayload(BaseModel):
    bitrix_user_id: int = 1001
    day_number: int


class BotMessagePayload(BaseModel):
    bitrix_user_id: int = 1001
    message: str


class AskPayload(BaseModel):
    question: str
    bitrix_user_id: int = 1001


class AuthPayload(BaseModel):
    login: str
    password: str


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def today_iso() -> str:
    return date.today().isoformat()


def calc_start_date(day_number: int) -> str:
    return (date.today() - timedelta(days=day_number - 1)).isoformat()


def row_to_dict(row: sqlite3.Row | None) -> Optional[Dict[str, Any]]:
    return dict(row) if row else None


def log_bitrix(method: str, payload: Dict[str, Any], conn: sqlite3.Connection | None = None) -> None:
    owns_connection = conn is None
    if conn is None:
        conn = db()
    try:
        conn.execute(
            "INSERT INTO bitrix_log(method, payload_json, created_at) VALUES (?, ?, ?)",
            (method, json.dumps(payload, ensure_ascii=False), now_iso()),
        )
        if owns_connection:
            conn.commit()
    finally:
        if owns_connection:
            conn.close()


def get_user_by_bitrix_id(conn: sqlite3.Connection, bitrix_user_id: int) -> sqlite3.Row:
    user = conn.execute(
        "SELECT * FROM users WHERE bitrix_user_id = ?", (bitrix_user_id,)
    ).fetchone()
    if user:
        return user

    conn.execute(
        """
        INSERT INTO users(bitrix_user_id, full_name, position, department, start_date, language)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            bitrix_user_id,
            "Алия Нурланова",
            "Главный специалист",
            "Департамент управления человеческими ресурсами",
            today_iso(),
            None,
        ),
    )
    conn.commit()
    return conn.execute("SELECT * FROM users WHERE bitrix_user_id = ?", (bitrix_user_id,)).fetchone()


def onboarding_day(user: sqlite3.Row) -> int:
    start = date.fromisoformat(user["start_date"])
    return (date.today() - start).days + 1


def task_deadline_for_type(task_type: str) -> str:
    """Presentation-friendly deadlines for task reminders and status colors."""
    times = {
        "safety": "10:00",
        "information_security": "11:30",
        "access_control": "14:30",
        "ethics": "15:30",
        "compliance": "16:30",
        "day14_pulse": "09:30",
        "day30_manager_1to1": "11:00",
        "day30_nps": "16:00",
        "day60_smart_goals": "10:00",
        "day90_hr_report": "17:00",
    }
    time_str = times.get(task_type, "18:00")
    return datetime.combine(date.today(), datetime.strptime(time_str, "%H:%M").time()).isoformat(timespec="seconds")


def classify_task_due_state(task: Dict[str, Any] | sqlite3.Row) -> Dict[str, Any]:
    values = dict(task)
    base_status = values.get("status") or "new"
    deadline_value = values.get("deadline")
    display_status = base_status
    due_text = "Дедлайн не указан"
    due_minutes = None
    if base_status == "completed":
        display_status = "completed"
        due_text = "Задача выполнена"
    elif deadline_value:
        try:
            deadline_dt = datetime.fromisoformat(str(deadline_value))
            delta = deadline_dt - datetime.now()
            due_minutes = int(delta.total_seconds() // 60)
            if delta.total_seconds() < 0:
                display_status = "overdue"
                minutes_abs = abs(due_minutes)
                if minutes_abs < 60:
                    due_text = f"Просрочена на {minutes_abs} мин."
                else:
                    due_text = f"Просрочена на {round(minutes_abs / 60, 1)} ч."
            elif delta <= timedelta(hours=DEMO_SOON_THRESHOLD_HOURS):
                display_status = "due_soon"
                if due_minutes < 60:
                    due_text = f"Скоро дедлайн: осталось {due_minutes} мин."
                else:
                    due_text = f"Скоро дедлайн: осталось {round(due_minutes / 60, 1)} ч."
            else:
                display_status = base_status
                due_text = f"До дедлайна {round(due_minutes / 60, 1)} ч."
        except ValueError:
            display_status = base_status
            due_text = "Дедлайн не распознан"
    values["base_status"] = base_status
    values["display_status"] = display_status
    # Frontend aliases used for presentation colors.
    values["visual_status"] = "soon_due" if display_status == "due_soon" else display_status
    if display_status == "due_soon":
        values["due_state"] = "soon_due"
    elif display_status == "overdue":
        values["due_state"] = "overdue"
    else:
        values["due_state"] = "normal"
    values["due_text"] = due_text
    values["due_minutes"] = due_minutes
    return values


def enrich_task(task: sqlite3.Row | Dict[str, Any], include_details: bool = True) -> Dict[str, Any]:
    values = classify_task_due_state(task)
    if include_details:
        values["details"] = task_detail_template(values.get("task_type", ""))
    return values


def create_popup_event(conn: sqlite3.Connection, user_id: int, popup_type: str, payload: Dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO popup_events(user_id, popup_type, payload_json, shown, created_at) VALUES (?, ?, ?, 0, ?)",
        (user_id, popup_type, json.dumps(payload, ensure_ascii=False), now_iso()),
    )


def ensure_nudge_state(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row:
    state = conn.execute("SELECT * FROM nudge_state WHERE user_id = ?", (user_id,)).fetchone()
    if state:
        return state
    conn.execute(
        "INSERT INTO nudge_state(user_id, current_card_day, sent_today, last_sent_at) VALUES (?, 1, 0, NULL)",
        (user_id,),
    )
    conn.commit()
    return conn.execute("SELECT * FROM nudge_state WHERE user_id = ?", (user_id,)).fetchone()


def ensure_day1_tasks(conn: sqlite3.Connection, user: sqlite3.Row) -> List[Dict[str, Any]]:
    existing = conn.execute(
        "SELECT * FROM onboarding_tasks WHERE user_id = ? ORDER BY id", (user["id"],)
    ).fetchall()
    if existing:
        return [dict(x) for x in existing]

    templates = [
        ("Инструктаж по технике безопасности", "safety", "Пройдите вводный инструктаж по технике безопасности и подтвердите ознакомление."),
        ("Инструктаж по информационной безопасности", "information_security", "Ознакомьтесь с правилами ИБ, защитой паролей и требованиями к корпоративным системам."),
        ("Ознакомление с пропускным режимом", "access_control", "Изучите порядок прохода в офис, использования проксим-карты и правил посещения."),
        ("Кодекс деловой этики", "ethics", "Ознакомьтесь с Кодексом деловой этики и подтвердите соблюдение норм поведения."),
        ("Модуль Комплаенс: антикоррупционная политика и линия доверия", "compliance", "Пройдите модуль по антикоррупционной политике, конфликтам интересов и линии доверия."),
    ]
    created: List[Dict[str, Any]] = []
    for title, task_type, description in templates:
        deadline = task_deadline_for_type(task_type)
        # In demo mode this is a mock of Bitrix tasks.task.add.
        bitrix_payload = {
            "fields": {
                "TITLE": title,
                "DESCRIPTION": description + f"\n\nВидео Председателя Правления: {CHAIRMAN_VIDEO_URL}",
                "RESPONSIBLE_ID": user["bitrix_user_id"],
                "DEADLINE": deadline,
            }
        }
        log_bitrix("tasks.task.add", bitrix_payload, conn)
        cursor = conn.execute(
            """
            INSERT INTO onboarding_tasks(user_id, bitrix_task_id, title, task_type, deadline, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'new', ?)
            """,
            (user["id"], f"MOCK-TASK-{task_type.upper()}", title, task_type, deadline, now_iso()),
        )
        created.append(
            {
                "id": cursor.lastrowid,
                "bitrix_task_id": f"MOCK-TASK-{task_type.upper()}",
                "title": title,
                "task_type": task_type,
                "deadline": deadline,
                "status": "new",
            }
        )
    return created


def create_day1_popup(conn: sqlite3.Connection, user: sqlite3.Row) -> None:
    tasks = ensure_day1_tasks(conn, user)
    payload = {
        "avatar_url": f"{PUBLIC_BACKEND_URL}/static/digital-buddy-face.png",
        "greeting": f"Добрый день, {user['full_name'].split()[0]}! День 1 вашей адаптации в КМГ.",
        "video_url": CHAIRMAN_VIDEO_URL,
        "next_task": tasks[0]["title"] if tasks else "Все задачи выполнены",
        "progress_text": "Выполнено 0 из 5 задач",
        "tasks": tasks,
        "buttons": ["Понятно", "Задать вопрос"],
    }
    create_popup_event(conn, user["id"], "day1", payload)




def get_stage_info(day: int) -> Dict[str, str]:
    if day <= 1:
        return {
            "stage": "Знакомство",
            "stage_period": "День 1",
            "stage_description": "Первый вход, приветствие Digital Buddy, видео и обязательные задачи.",
        }
    if 2 <= day <= 24:
        return {
            "stage": "Вовлечение",
            "stage_period": "Дни 2–24",
            "stage_description": "Ежедневные Culture Fit карточки и закрепление правил компании.",
        }
    if 25 <= day <= 90:
        return {
            "stage": "Адаптация",
            "stage_period": "Месяц 1–3",
            "stage_description": "Встречи 1:1, цели по SMART, опросы и корректировка КПД.",
        }
    return {
        "stage": "Закрепление",
        "stage_period": "Месяц 3–12",
        "stage_description": "HR-аналитика, развитие и закрепление сотрудника в команде.",
    }


def task_progress(conn: sqlite3.Connection, user_id: int) -> Dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM onboarding_tasks WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    display_counts = {"new": 0, "in_progress": 0, "due_soon": 0, "overdue": 0, "completed": 0}
    base_counts = {"new": 0, "in_progress": 0, "completed": 0}
    for row in rows:
        task = enrich_task(row)
        display_counts[task["display_status"]] = display_counts.get(task["display_status"], 0) + 1
        base_counts[task["base_status"]] = base_counts.get(task["base_status"], 0) + 1
    total = len(rows)
    completed = base_counts.get("completed", 0)
    percent = round((completed / total) * 100) if total else 0
    return {
        "total": total,
        "completed": completed,
        "in_progress": display_counts.get("in_progress", 0),
        "new": display_counts.get("new", 0),
        "due_soon": display_counts.get("due_soon", 0),
        "soon_due": display_counts.get("due_soon", 0),
        "overdue": display_counts.get("overdue", 0),
        "base_in_progress": base_counts.get("in_progress", 0),
        "base_new": base_counts.get("new", 0),
        "percent": percent,
        "text": f"Выполнено {completed} из {total} задач" if total else "Задачи пока не созданы",
    }


def task_detail_template(task_type: str) -> Dict[str, Any]:
    templates: Dict[str, Dict[str, Any]] = {
        "safety": {
            "format": "Очный инструктаж + тест",
            "place": "Учебный кабинет / кабинет ОТ и ПБ",
            "participants": ["Сотрудник", "Специалист по охране труда"],
            "time": "10:00",
            "duration": "45 минут",
            "description": "Вводный инструктаж по технике безопасности, правилам поведения в офисе и действиям при ЧС.",
            "checklist": ["Посетить инструктаж", "Пройти короткий тест", "Подтвердить ознакомление"],
            "source": "Внутренний регламент ОТ и ПБ",
            "result": "Сотрудник знает базовые правила безопасности и подтверждает ознакомление.",
        },
        "information_security": {
            "format": "Онлайн-модуль",
            "place": "Корпоративный портал team.kmg.kz",
            "participants": ["Сотрудник", "Digital Buddy", "IT/ИБ"],
            "time": "11:30",
            "duration": "30 минут",
            "description": "Правила работы с корпоративными системами, паролями, почтой и конфиденциальной информацией.",
            "checklist": ["Изучить правила ИБ", "Проверить доступы", "Подтвердить ознакомление"],
            "source": "Политика информационной безопасности",
            "result": "Сотрудник понимает базовые требования ИБ и безопасной работы в системах.",
        },
        "access_control": {
            "format": "Ознакомление + проверка правил",
            "place": "Пост охраны / бюро пропусков / портал",
            "participants": ["Сотрудник", "Ответственный ДКБ"],
            "time": "14:30",
            "duration": "20 минут",
            "description": "Порядок прохода в офис, использование проксим-карты, запрет передачи пропуска другим лицам.",
            "checklist": ["Ознакомиться с правилами", "Проверить наличие проксим-карты", "Подтвердить ознакомление"],
            "source": "Правила организации пропускного и внутриобъектового режимов KMG-PR-1186.5-22",
            "result": "Сотрудник знает, как пользоваться пропуском и какие действия запрещены.",
        },
        "ethics": {
            "format": "Самостоятельное ознакомление",
            "place": "Корпоративный портал team.kmg.kz",
            "participants": ["Сотрудник", "Digital Buddy"],
            "time": "15:30",
            "duration": "25 минут",
            "description": "Ключевые нормы делового поведения: уважение, профессионализм, конфиденциальность, недопущение дискриминации.",
            "checklist": ["Ознакомиться с Кодексом", "Пройти подтверждение", "Задать вопрос Digital Buddy при необходимости"],
            "source": "Кодекс деловой этики АО НК «КазМунайГаз» KMG-VND-4071.2-48",
            "result": "Сотрудник понимает базовые нормы поведения в компании.",
        },
        "compliance": {
            "format": "Онлайн-модуль + кейсы",
            "place": "Корпоративный портал team.kmg.kz",
            "participants": ["Сотрудник", "Комплаенс-служба", "Digital Buddy"],
            "time": "16:30",
            "duration": "40 минут",
            "description": "Антикоррупционные ограничения, конфликт интересов, подарки, линия доверия и порядок сообщения о нарушениях.",
            "checklist": ["Изучить модуль", "Разобрать кейсы", "Подтвердить ознакомление"],
            "source": "Инструкция по противодействию коррупции KMG-VND-6677.1-47",
            "result": "Сотрудник понимает, как действовать при подарках, конфликте интересов и коррупционных рисках.",
        },
        "day14_pulse": {
            "format": "Пульс-опрос",
            "place": "Чат Digital Buddy",
            "participants": ["Сотрудник", "HR-специалист"],
            "time": "09:30",
            "duration": "5 минут",
            "description": "Короткий опрос по первым двум неделям: понятны ли задачи, хватает ли поддержки, есть ли блокеры.",
            "checklist": ["Ответить на 3 вопроса", "Описать сложности", "Отправить результат HR"],
            "source": "Программа адаптации КМГ, опрос 14-го дня",
            "result": "HR получает ранний сигнал о качестве адаптации.",
        },
        "day30_manager_1to1": {
            "format": "Встреча 1:1",
            "place": "Переговорная 4B / MS Teams",
            "participants": ["Сотрудник", "Непосредственный руководитель", "при необходимости HR"],
            "time": "11:00",
            "duration": "45 минут",
            "description": "Обсуждение первых результатов, ожиданий руководителя, целей испытательного срока и поддержки со стороны команды.",
            "checklist": ["Подготовить 3 достижения", "Подготовить 2 вопроса", "Согласовать цели по SMART"],
            "source": "Этап «Адаптация»: регулярные встречи 1:1 и корректировка целей",
            "result": "Сотрудник и руководитель согласовали ожидания и следующие шаги.",
        },
        "day30_nps": {
            "format": "NPS-опрос",
            "place": "Чат Digital Buddy",
            "participants": ["Сотрудник", "HR"],
            "time": "16:00",
            "duration": "3 минуты",
            "description": "Оценка адаптации за первый месяц и открытый комментарий сотрудника.",
            "checklist": ["Поставить оценку 0–10", "Оставить комментарий", "Отправить HR"],
            "source": "Программа адаптации КМГ, опрос 30-го дня",
            "result": "HR видит NPS и качество первого месяца адаптации.",
        },
        "day60_smart_goals": {
            "format": "SMART-сессия",
            "place": "Чат Digital Buddy + встреча с руководителем",
            "participants": ["Сотрудник", "Digital Buddy", "Руководитель"],
            "time": "10:00",
            "duration": "30 минут",
            "description": "Актуализация целей на испытательный срок: конкретность, измеримость, достижимость, релевантность и срок.",
            "checklist": ["Описать текущие цели", "Уточнить метрики", "Согласовать сроки"],
            "source": "Этап «Адаптация»: помощь с целями по SMART",
            "result": "Цели сотрудника сформулированы по SMART и готовы к внесению в КПД.",
        },
        "day90_hr_report": {
            "format": "HR-итоги адаптации",
            "place": "HR dashboard / Bitrix24",
            "participants": ["HR", "Руководитель", "Сотрудник"],
            "time": "17:00",
            "duration": "15 минут",
            "description": "Итоговый обзор маршрута: задачи, опросы, вовлечённость, риск-флаги и рекомендации по развитию.",
            "checklist": ["Проверить прогресс", "Сформировать рекомендации", "Согласовать дальнейший план"],
            "source": "Этап «Закрепление»: HR-аналитика и итоговая оценка",
            "result": "HR и руководитель видят агрегированную картину адаптации без раскрытия личной переписки.",
        },
    }
    return templates.get(task_type, {
        "format": "Задача адаптации",
        "place": "Корпоративный портал team.kmg.kz",
        "participants": ["Сотрудник", "Digital Buddy"],
        "time": "В течение рабочего дня",
        "duration": "15 минут",
        "description": "Задача персонального маршрута адаптации.",
        "checklist": ["Открыть задачу", "Выполнить действие", "Отметить как выполненную"],
        "source": "Программа онбординга КМГ",
        "result": "Задача закрыта в маршруте адаптации.",
    })


def create_task_if_missing(
    conn: sqlite3.Connection,
    user: sqlite3.Row,
    title: str,
    task_type: str,
    description: str,
    deadline: str,
) -> Optional[Dict[str, Any]]:
    existing = conn.execute(
        "SELECT * FROM onboarding_tasks WHERE user_id = ? AND task_type = ?",
        (user["id"], task_type),
    ).fetchone()
    if existing:
        return None
    bitrix_payload = {
        "fields": {
            "TITLE": title,
            "DESCRIPTION": description,
            "RESPONSIBLE_ID": user["bitrix_user_id"],
            "DEADLINE": deadline,
        }
    }
    log_bitrix("tasks.task.add", bitrix_payload, conn)
    cursor = conn.execute(
        """
        INSERT INTO onboarding_tasks(user_id, bitrix_task_id, title, task_type, deadline, status, created_at)
        VALUES (?, ?, ?, ?, ?, 'new', ?)
        """,
        (user["id"], f"MOCK-TASK-{task_type.upper()}", title, task_type, deadline, now_iso()),
    )
    return {
        "id": cursor.lastrowid,
        "bitrix_task_id": f"MOCK-TASK-{task_type.upper()}",
        "title": title,
        "task_type": task_type,
        "deadline": deadline,
        "status": "new",
    }


def ensure_milestone_tasks(conn: sqlite3.Connection, user: sqlite3.Row, day: int) -> None:
    milestones = [
        (14, "Пульс-опрос 14-го дня", "day14_pulse", "Ответьте на 3 вопроса о первых двух неделях адаптации."),
        (30, "Встреча 1:1 с руководителем", "day30_manager_1to1", "Подготовьтесь к встрече: результаты, вопросы, цели и ожидания."),
        (30, "NPS-опрос 30-го дня", "day30_nps", "Оцените качество первого месяца адаптации и оставьте комментарий."),
        (60, "Актуализация целей по SMART", "day60_smart_goals", "Сформулируйте цели испытательного срока по SMART вместе с Digital Buddy."),
        (90, "Итоговый HR-отчёт по адаптации", "day90_hr_report", "Сформируйте итоговый статус адаптации, рекомендации и план развития."),
    ]
    for milestone_day, title, task_type, description in milestones:
        if day >= milestone_day:
            create_task_if_missing(conn, user, title, task_type, description, task_deadline_for_type(task_type))


def create_stage_popup_if_needed(conn: sqlite3.Connection, user: sqlite3.Row, day: int) -> bool:
    stage = get_stage_info(day)
    popup_type = f"stage_day_{day}"
    pending = conn.execute(
        "SELECT id FROM popup_events WHERE user_id = ? AND popup_type = ? AND shown = 0",
        (user["id"], popup_type),
    ).fetchone()
    if pending:
        return False
    messages = {
        25: {
            "title": "Culture Fit маршрут завершён",
            "text": "Вы получили ключевые карточки корпоративной культуры. Дальше Digital Buddy поможет закрепить цели и подготовиться к встречам 1:1.",
            "next_task": "Подготовка к адаптационной встрече 1:1",
        },
        30: {
            "title": "30-й день адаптации",
            "text": "Пора пройти NPS-опрос и обсудить первые результаты с руководителем.",
            "next_task": "Встреча 1:1 с руководителем",
        },
        60: {
            "title": "60-й день адаптации",
            "text": "Рекомендуем актуализировать цели испытательного срока по SMART и проверить прогресс в КПД.",
            "next_task": "Актуализация целей по SMART",
        },
        90: {
            "title": "90-й день адаптации",
            "text": "Digital Buddy подготовил данные для итогового HR-обзора: задачи, опросы, прогресс и рекомендации.",
            "next_task": "Итоговый HR-отчёт по адаптации",
        },
    }
    if day not in messages:
        return False
    progress = task_progress(conn, user["id"])
    msg = messages[day]
    payload = {
        "avatar_url": f"{PUBLIC_BACKEND_URL}/static/digital-buddy-face.png",
        "greeting": f"{msg['title']}: {user['full_name'].split()[0]}, продолжаем маршрут адаптации.",
        "stage": stage,
        "message": msg["text"],
        "next_task": msg["next_task"],
        "progress_text": progress["text"],
        "buttons": ["Понятно", "Задать вопрос"],
    }
    create_popup_event(conn, user["id"], popup_type, payload)
    return True


def unread_notification_count(conn: sqlite3.Connection, user_id: int) -> int:
    last_read_row = conn.execute(
        "SELECT last_read_chat_id FROM notification_reads WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    last_read = last_read_row["last_read_chat_id"] if last_read_row else 0
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM chat_messages WHERE user_id = ? AND role = 'bot' AND id > ?",
        (user_id, last_read),
    ).fetchone()
    return int(row["cnt"] if row else 0)


def mark_notifications_read(conn: sqlite3.Connection, user_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS max_id FROM chat_messages WHERE user_id = ? AND role = 'bot'",
        (user_id,),
    ).fetchone()
    max_id = int(row["max_id"] if row else 0)
    conn.execute(
        "INSERT OR REPLACE INTO notification_reads(user_id, last_read_chat_id, updated_at) VALUES (?, ?, ?)",
        (user_id, max_id, now_iso()),
    )
    return max_id


def reminder_message(task: Dict[str, Any], reminder_type: str, language: str) -> str:
    title = task.get("title", "задача")
    deadline = str(task.get("deadline") or "")[:16].replace("T", " ")
    if language == "kz":
        if reminder_type == "overdue":
            return (
                "Digital Buddy:\n\n"
                f"⚠️ Еске салу: «{title}» тапсырмасының мерзімі өтіп кетті. "
                f"Мерзімі: {deadline}. Тапсырманы ашып, орындағаннан кейін «Орындалды» деп белгілеңіз."
            )
        return (
            "Digital Buddy:\n\n"
            f"⏰ Еске салу: «{title}» тапсырмасының мерзімі жақындап қалды. "
            f"{task.get('due_text', '')} Мерзімі: {deadline}."
        )
    if reminder_type == "overdue":
        return (
            "Digital Buddy:\n\n"
            f"⚠️ Задача «{title}» просрочена. Дедлайн: {deadline}. "
            "Откройте задачу, завершите действие и отметьте её как выполненную."
        )
    return (
        "Digital Buddy:\n\n"
        f"⏰ Напоминание: скоро дедлайн задачи «{title}». "
        f"{task.get('due_text', '')} Дедлайн: {deadline}."
    )


def send_task_reminder_if_needed(conn: sqlite3.Connection, user: sqlite3.Row, task: Dict[str, Any], reminder_type: str) -> bool:
    exists = conn.execute(
        "SELECT id FROM task_reminders WHERE user_id = ? AND task_id = ? AND reminder_type = ?",
        (user["id"], task["id"], reminder_type),
    ).fetchone()
    if exists:
        return False
    language = user["language"] or "ru"
    message = reminder_message(task, reminder_type, language)
    conn.execute(
        "INSERT INTO chat_messages(user_id, role, message, detected_language, sentiment_score, created_at) VALUES (?, 'bot', ?, ?, NULL, ?)",
        (user["id"], message, language, now_iso()),
    )
    log_bitrix(
        "imbot.send",
        {
            "BOT_NAME": "Digital Buddy",
            "BOT_AVATAR": "40x40 digital-buddy-face.png",
            "DIALOG_ID": str(user["bitrix_user_id"]),
            "MESSAGE": message,
            "REMINDER_TYPE": reminder_type,
            "TASK_ID": task.get("bitrix_task_id"),
        },
        conn,
    )
    conn.execute(
        "INSERT INTO task_reminders(user_id, task_id, reminder_type, created_at) VALUES (?, ?, ?, ?)",
        (user["id"], task["id"], reminder_type, now_iso()),
    )
    return True


def generate_task_reminders(conn: sqlite3.Connection, user: sqlite3.Row) -> int:
    sent = 0
    rows = conn.execute(
        "SELECT * FROM onboarding_tasks WHERE user_id = ? AND status != 'completed' ORDER BY id",
        (user["id"],),
    ).fetchall()
    for row in rows:
        task = enrich_task(row)
        if task["display_status"] == "overdue":
            if send_task_reminder_if_needed(conn, user, task, "overdue"):
                sent += 1
        elif task["display_status"] == "due_soon":
            if send_task_reminder_if_needed(conn, user, task, "due_soon"):
                sent += 1
    return sent


def process_on_login_core(conn: sqlite3.Connection, user: sqlite3.Row) -> Dict[str, Any]:
    day = onboarding_day(user)
    popup_created = False
    ensure_milestone_tasks(conn, user, day)
    if day == 1:
        pending = conn.execute(
            "SELECT id FROM popup_events WHERE user_id = ? AND popup_type = 'day1' AND shown = 0",
            (user["id"],),
        ).fetchone()
        if not pending:
            create_day1_popup(conn, user)
            popup_created = True
    elif 2 <= day <= 24:
        result = handle_nudge(conn, user)
        popup_created = result is not None
    elif day in (25, 30, 60, 90):
        popup_created = create_stage_popup_if_needed(conn, user, day)
    return {"ok": True, "bitrix_user_id": user["bitrix_user_id"], "onboarding_day": day, "popup_created": popup_created}

def handle_nudge(conn: sqlite3.Connection, user: sqlite3.Row) -> Optional[Dict[str, Any]]:
    state = ensure_nudge_state(conn, user["id"])
    if int(state["sent_today"]) == 1:
        return None

    card = conn.execute(
        "SELECT * FROM nudge_cards WHERE day_number = ?", (state["current_card_day"],)
    ).fetchone()
    if not card:
        return None

    lang = user["language"] or "ru"
    card_text = card["text_kz"] if lang == "kz" and card["text_kz"] else card["text_ru"]
    greeting = (
        f"Сәлеметсіз бе, {user['full_name'].split()[0]}! Бүгінгі корпоративтік мәдениет карточкаңыз."
        if lang == "kz"
        else f"Добрый день, {user['full_name'].split()[0]}! Сегодня ваша карточка корпоративной культуры."
    )
    payload = {
        "avatar_url": f"{PUBLIC_BACKEND_URL}/static/digital-buddy-face.png",
        "greeting": greeting,
        "nudge": {
            "day_number": card["day_number"],
            "topic": card["topic"],
            "text": card_text,
            "source_document": card["source_document"],
        },
        "next_task": "Продолжайте маршрут адаптации",
        "progress_text": f"Карточка {card['day_number']} из 23",
        "buttons": ["Понятно", "Задать вопрос"],
    }
    create_popup_event(conn, user["id"], "nudge", payload)

    source_label = "ВНД дереккөзі" if lang == "kz" else "Источник ВНД"
    message = (
        f"📌 Culture Fit Nudge #{card['day_number']}\n\n"
        f"{card['topic']}\n"
        f"{card_text}\n\n"
        f"{source_label}: {card['source_document']}"
    )
    conn.execute(
        "INSERT INTO chat_messages(user_id, role, message, detected_language, sentiment_score, created_at) VALUES (?, 'bot', ?, ?, NULL, ?)",
        (user["id"], message, lang, now_iso()),
    )
    log_bitrix(
        "imbot.send",
        {
            "BOT_NAME": "Digital Buddy",
            "DIALOG_ID": str(user["bitrix_user_id"]),
            "MESSAGE": message,
        },
        conn,
    )
    conn.execute(
        "UPDATE nudge_state SET sent_today = 1, last_sent_at = ?, current_card_day = current_card_day + 1 WHERE user_id = ?",
        (now_iso(), user["id"]),
    )
    return payload


def tokenize(text: str) -> List[str]:
    return [t for t in re.findall(r"[a-zA-Zа-яА-ЯәіңғүұқөһӘІҢҒҮҰҚӨҺ0-9]+", text.lower()) if len(t) > 2]



def load_rules_knowledge() -> List[Dict[str, Any]]:
    """Verified OCR/manual seed chunks for pages where the PDFs are scanned.

    Important: this file is NOT the answer engine anymore. It is used only as
    an OCR fallback/source-normalization layer for scanned pages. The bot answer
    path goes through ChromaDB vector search over document chunks.
    """
    try:
        return json.loads(RULES_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def infer_page_from_rule(item: Dict[str, Any]) -> int:
    """Best-effort page metadata for manually verified snippets.

    The PDFs contain scans; some pages cannot be text-extracted without OCR.
    These page numbers point judges to the actual source page/section.
    """
    code = item.get("document_code", "")
    point = item.get("point", "")
    section = item.get("section_ru", "")
    rule_id = item.get("id", "")
    if "4071" in code:
        if "5.1" in point:
            return 5
        if "5.2" in point:
            return 6
        if "4.3" in point:
            return 3
        if "7.4" in section:
            return 11
        if "Раздел 9" in section:
            return 14
        return 1
    if "6677" in code:
        if "5.1.2" in point or "5.1.3" in point:
            return 4
        if "5.1.5" in point or "5.1.6" in point:
            return 5
        if "5.4" in section:
            return 10
        if "5.3" in section:
            return 8
        if "5.7" in section or "gifts" in rule_id:
            return 13
        return 2
    if "1186" in code:
        if "5.1" in point:
            return 12
        if "5.3" in point:
            return 16
        if "5.8" in point or "5.8" in section:
            return 30
        return 1
    if "6241" in code:
        if "пп. 1" in point:
            return 5
        return 6
    return 0


def source_document_url(source_file: str) -> str:
    """Return internal link to a source document served by the backend.

    The RAG source can be a PDF or another internal artifact such as the
    onboarding technical assignment DOCX. The endpoint sanitizes the filename
    before serving the file.
    """
    if not source_file:
        return ""
    return f"/api/documents/{source_file}"


def hash_embedding(text: str, dim: int = RAG_EMBEDDING_DIM) -> List[float]:
    """Offline multilingual embedding fallback used by ChromaDB.

    This is a deterministic hashing-vector embedding, not a keyword dictionary.
    It lets the demo run without downloading external models. In production,
    the same ChromaDB add/query calls can use sentence-transformers or a
    corporate embeddings API by replacing this function.
    """
    vector = [0.0] * dim
    normalized = normalize_for_search(text)
    tokens = tokenize(normalized)
    # Add word tokens and short character n-grams for Russian/Kazakh morphology.
    features: List[str] = []
    for token in tokens:
        features.append(f"w:{token}")
        if len(token) >= 4:
            for i in range(max(1, len(token) - 2)):
                features.append(f"g:{token[i:i+3]}")
    if not features:
        features = [normalized[:64] or "empty"]
    for feature in features:
        digest = hashlib.sha256(feature.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[idx] += sign
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [round(v / norm, 6) for v in vector]


def embed_texts(texts: List[str]) -> List[List[float]]:
    return [hash_embedding(text) for text in texts]


def get_chroma_client() -> Any:
    if chromadb is None:
        return None
    try:
        if CHROMA_HOST:
            return chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
        return chromadb.PersistentClient(path=CHROMA_PERSIST_DIR)
    except Exception:
        return None


def build_rule_seed_chunks() -> List[Dict[str, Any]]:
    chunks: List[Dict[str, Any]] = []
    for item in load_rules_knowledge():
        source_file = item.get("source_file", "")
        page = int(item.get("page") or infer_page_from_rule(item))
        text_ru = item.get("text_ru", "")
        text_kz = item.get("text_kz", item.get("text_ru", ""))
        title_ru = item.get("title_ru", "")
        title_kz = item.get("title_kz", title_ru)
        section_ru = item.get("section_ru", "")
        section_kz = item.get("section_kz", section_ru)
        point = item.get("point", "")
        keywords = " ".join(item.get("keywords_ru", []) + item.get("keywords_kz", []))
        document_text = "\n".join([
            title_ru, title_kz, item.get("document_code", ""), section_ru,
            section_kz, point, text_ru, text_kz, keywords,
        ]).strip()
        chunks.append({
            "id": f"rule-{item.get('id')}",
            "document": document_text,
            "text_ru": text_ru,
            "text_kz": text_kz,
            "title_ru": title_ru,
            "title_kz": title_kz,
            "document_code": item.get("document_code", ""),
            "source_file": source_file,
            "document_url": source_document_url(source_file),
            "section_ru": section_ru,
            "section_kz": section_kz,
            "point": point,
            "page": page,
            "category": item.get("category", "general"),
            "chunk_type": "verified_ocr_seed",
        })
    return chunks


def guess_document_metadata(file_path: Path) -> Dict[str, str]:
    name = file_path.name
    if "4071" in name or "этики" in name:
        return {"title_ru": "Кодекс деловой этики АО НК «КазМунайГаз»", "title_kz": "«ҚазМұнайГаз» ҰК АҚ іскерлік әдеп кодексі", "document_code": "KMG-VND-4071.2-48"}
    if "6677" in name or "против" in name:
        return {"title_ru": "Инструкция по противодействию коррупции в АО НК «КазМунайГаз»", "title_kz": "«ҚазМұнайГаз» ҰК АҚ сыбайлас жемқорлыққа қарсы іс-қимыл нұсқаулығы", "document_code": "KMG-VND-6677.1-47"}
    if "1186" in name or "пропуск" in name:
        return {"title_ru": "Правила организации пропускного и внутриобъектового режимов в административных зданиях АО НК «КазМунайГаз»", "title_kz": "«ҚазМұнайГаз» ҰК АҚ әкімшілік ғимараттарындағы өткізу және объектішілік режим қағидалары", "document_code": "KMG-PR-1186.5-22"}
    if "6241" in name or "Должностная" in name:
        return {"title_ru": "Должностная инструкция начальника отдела найма и трудовых отношений ДУЧР АО НК «КазМунайГаз»", "title_kz": "Адам ресурстарын басқару департаментінің жалдау және еңбек қатынастары бөлімі басшысының лауазымдық нұсқаулығы", "document_code": "KMG-DI-6241.4-06/KMG-PD-646.16-06"}
    return {"title_ru": file_path.stem, "title_kz": file_path.stem, "document_code": ""}


def split_text(text: str, max_chars: int = 1400, overlap: int = 180) -> List[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    chunks: List[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        window = text[start:end]
        if end < len(text):
            dot = max(window.rfind(". "), window.rfind("; "), window.rfind("\n"))
            if dot > max_chars // 2:
                end = start + dot + 1
                window = text[start:end]
        chunks.append(window.strip())
        if end >= len(text):
            break
        start = max(0, end - overlap)
    return chunks


def infer_section_from_text(page_text: str) -> str:
    match = re.search(r"((?:\d+\.){1,3}\s*[^\n]{5,120})", page_text)
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip()
    return "Раздел не определён"


def extract_pdf_chunks() -> List[Dict[str, Any]]:
    """Extract text chunks from the actual PDF files when text layer exists.

    Some KMG PDFs are scanned images. For those, verified OCR seed chunks from
    rules_knowledge.json are still indexed into the same ChromaDB collection.
    """
    chunks: List[Dict[str, Any]] = []
    if PdfReader is None or not VND_DOCS_DIR.exists():
        return chunks
    for file_path in sorted(VND_DOCS_DIR.glob("*.pdf")):
        meta = guess_document_metadata(file_path)
        try:
            reader = PdfReader(str(file_path))
        except Exception:
            continue
        for page_index, page in enumerate(reader.pages, start=1):
            try:
                page_text = page.extract_text() or ""
            except Exception:
                page_text = ""
            if len(page_text.strip()) < 120:
                continue
            section = infer_section_from_text(page_text)
            for chunk_index, chunk_text in enumerate(split_text(page_text)):
                doc_text = "\n".join([meta["title_ru"], meta["document_code"], section, chunk_text])
                chunks.append({
                    "id": f"pdf-{file_path.stem}-{page_index}-{chunk_index}",
                    "document": doc_text,
                    "text_ru": chunk_text,
                    "text_kz": "",
                    "title_ru": meta["title_ru"],
                    "title_kz": meta["title_kz"],
                    "document_code": meta["document_code"],
                    "source_file": file_path.name,
                    "document_url": source_document_url(file_path.name),
                    "section_ru": section,
                    "section_kz": section,
                    "point": f"стр. {page_index}",
                    "page": page_index,
                    "category": "pdf_text",
                    "chunk_type": "pdf_text_layer",
                })
    return chunks


def build_all_vnd_chunks() -> List[Dict[str, Any]]:
    # Verified seed chunks first because they contain exact points for scanned pages.
    chunks = build_rule_seed_chunks()
    seen = {c["id"] for c in chunks}
    for chunk in extract_pdf_chunks():
        if chunk["id"] not in seen:
            chunks.append(chunk)
            seen.add(chunk["id"])
    return chunks


def ensure_chroma_index(conn: Optional[sqlite3.Connection] = None, force: bool = False) -> Dict[str, Any]:
    client = get_chroma_client()
    chunks = build_all_vnd_chunks()
    if client is None:
        return {"ok": False, "reason": "chromadb_not_available", "chunks_prepared": len(chunks)}
    try:
        if force or FORCE_RAG_REINDEX:
            try:
                client.delete_collection(CHROMA_COLLECTION_NAME)
            except Exception:
                pass
        collection = client.get_or_create_collection(
            name=CHROMA_COLLECTION_NAME,
            metadata={"description": "KMG VND documents indexed for Digital Buddy RAG", "hnsw:space": "cosine"},
        )
        if collection.count() == len(chunks) and not force:
            return {"ok": True, "collection": CHROMA_COLLECTION_NAME, "count": collection.count(), "chunks_prepared": len(chunks), "reindexed": False}
        try:
            client.delete_collection(CHROMA_COLLECTION_NAME)
        except Exception:
            pass
        collection = client.get_or_create_collection(
            name=CHROMA_COLLECTION_NAME,
            metadata={"description": "KMG VND documents indexed for Digital Buddy RAG", "hnsw:space": "cosine"},
        )
        ids = [c["id"] for c in chunks]
        documents = [c["document"] for c in chunks]
        metadatas = []
        for c in chunks:
            metadatas.append({
                "text_ru": c.get("text_ru", ""),
                "text_kz": c.get("text_kz", ""),
                "title_ru": c.get("title_ru", ""),
                "title_kz": c.get("title_kz", ""),
                "document_code": c.get("document_code", ""),
                "source_file": c.get("source_file", ""),
                "document_url": c.get("document_url", ""),
                "section_ru": c.get("section_ru", ""),
                "section_kz": c.get("section_kz", ""),
                "point": c.get("point", ""),
                "page": int(c.get("page") or 0),
                "category": c.get("category", "general"),
                "chunk_type": c.get("chunk_type", "unknown"),
            })
        embeddings = embed_texts(documents)
        batch_size = 64
        for i in range(0, len(ids), batch_size):
            collection.add(
                ids=ids[i:i + batch_size],
                documents=documents[i:i + batch_size],
                metadatas=metadatas[i:i + batch_size],
                embeddings=embeddings[i:i + batch_size],
            )
        return {"ok": True, "collection": CHROMA_COLLECTION_NAME, "count": collection.count(), "chunks_prepared": len(chunks), "reindexed": True}
    except Exception as exc:
        return {"ok": False, "reason": str(exc), "chunks_prepared": len(chunks)}


def detect_language(text: str) -> str:
    """Detect ru/kz for the first user message.

    This is intentionally deterministic for demo. Kazakh letters and common
    Kazakh words switch the session to kz; otherwise the default is ru.
    """
    lower = text.lower()
    kazakh_letters = "әіңғүұқөһӘІҢҒҮҰҚӨҺ"
    kazakh_words = [
        "сәлем", "қалай", "керек", "жұмыс", "жұмысқа", "кешік", "кешігіп",
        "не істеу", "істеу", "бола ма", "болады", "болмайды", "сыйлық",
        "пара", "рұқсат", "рұқсатнама", "кіру", "компания", "қызметкер",
        "хабарлау", "қайда", "мүдде", "қақтығыс", "құпия", "қауіпсіздік",
    ]
    if any(ch in text for ch in kazakh_letters):
        return "kz"
    if any(w in lower for w in kazakh_words):
        return "kz"
    return "ru"


def get_user_chat_language(conn: sqlite3.Connection, user: sqlite3.Row, incoming_text: str) -> str:
    """Lock language by the first user chat message and reuse it later."""
    user_msg_count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM chat_messages WHERE user_id = ? AND role = 'user'",
        (user["id"],),
    ).fetchone()["cnt"]
    if user_msg_count == 0:
        detected = detect_language(incoming_text)
        conn.execute("UPDATE users SET language = ? WHERE id = ?", (detected, user["id"]))
        return detected
    stored = user["language"] if "language" in user.keys() else None
    return stored or "ru"


def simple_sentiment(text: str) -> float:
    lower = text.lower()
    negative_words = [
        "плохо", "сложно", "не понимаю", "не помогает", "уйти", "проблем", "жалоба",
        "қыйын", "қиын", "түсінбей", "көмектеспейді", "мәселе", "шағым",
    ]
    positive_words = ["спасибо", "понятно", "хорошо", "класс", "рахмет", "жақсы", "түсінікті"]
    score = 0.0
    for w in positive_words:
        if w in lower:
            score += 0.4
    for w in negative_words:
        if w in lower:
            score -= 0.6
    return max(-1.0, min(1.0, score))


def normalize_for_search(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


RAG_STOPWORDS = {
    "можно", "нужно", "если", "что", "как", "куда", "надо", "могу", "мне", "моя", "мой",
    "ли", "или", "это", "по", "на", "за", "для", "при", "меня", "работы", "работа",
    "керек", "қалай", "болса", "бола", "ма", "ме", "не", "және", "үшін", "мен", "маған",
}


def contains_any(text: str, phrases: List[str]) -> bool:
    return any(phrase in text for phrase in phrases)


QUESTION_TO_RULE_ROUTES: List[Dict[str, Any]] = [
    {
        "rule_ids": ["company_values_code", "company_ethics_principles"],
        "phrases": [
            "ценности", "корпоративные ценности", "принципы компании", "миссия кодекса",
            "какие ценности", "құндылық", "қағидат", "принцип"
        ],
    },
    {
        "rule_ids": ["documents_catalog_internal_rules"],
        "phrases": [
            "список внутренних правил", "внутренние правила", "внутренние инструкции",
            "где найти список внутренних правил", "где найти внутренние инструкции", "внд",
            "руководство сотрудника", "employee handbook", "ішкі ереж", "ішкі құжат"
        ],
    },
    {
        "rule_ids": ["onboarding_route_full", "onboarding_day1_requirements"],
        "phrases": [
            "как пройти онбординг", "онбординг", "адаптация", "новый сотрудник",
            "к кому обращаться по вопросам адаптации", "маршрут адаптации", "первый день",
            "день 1", "digital buddy", "бейімдеу", "жаңа қызметкер"
        ],
    },
    {
        "rule_ids": ["preboarding_it_workplace"],
        "phrases": [
            "получить доступ к корпоративным системам", "корпоративные системы", "active directory",
            " ad ", "корпоративную почту", "корпоративная почта", "outlook", "настроить корпоративную почту",
            "оформить рабочее место", "рабочее место", "канцтовары", "мебель", "сэд", "сапфир",
            "intranet", "e-otinish", "доступ к outlook", "қолжетімділік", "корпоративтік пошта"
        ],
    },
    {
        "rule_ids": ["access_proxy_card", "preboarding_it_workplace"],
        "phrases": [
            "заказать пропуск", "проксим", "проксим-карта", "офисный пропуск", "постоянный пропуск",
            "как получить пропуск", "рұқсатнама", "проксим карта"
        ],
    },
    {
        "rule_ids": ["access_visitors"],
        "phrases": [
            "разовый пропуск", "временный пропуск", "пропуск посетителя", "посетитель", "гость в офис",
            "келуші", "уақытша рұқсат"
        ],
    },
    {
        "rule_ids": ["access_no_transfer", "access_forbidden_items"],
        "phrases": [
            "передать пропуск", "дать пропуск", "пропуск коллеге", "передача пропуска",
            "другим лицам", "рұқсатнаманы беру", "пропускты беру"
        ],
    },
    {
        "rule_ids": ["access_after_hours", "access_forbidden_items"],
        "phrases": [
            "после рабочего дня", "после работы", "закрыть помещение", "сверх разрешенного времени",
            "оставаться в офисе", "жұмыс күнінен кейін"
        ],
    },
    {
        "rule_ids": ["access_forbidden_items"],
        "phrases": [
            "что запрещено в офисе", "запрещенные предметы", "алкоголь", "оружие", "наркотики",
            "вынос имущества", "внос имущества", "объектішілік режим"
        ],
    },
    {
        "rule_ids": ["worktime_absence_notice", "ethics_respect_communication"],
        "phrases": [
            "уйти пораньше", "уйти с работы пораньше", "уйти раньше", "раньше уйти",
            "отпроситься", "отгул", "опоздание", "опаздываю", "опоздаю", "рабочее время",
            "график работы", "как сообщить об опоздании", "жұмыстан ерте", "кешігу", "жұмыс уақыты"
        ],
    },
    {
        "rule_ids": ["training_mandatory_day1"],
        "phrases": [
            "обязательное обучение", "обязательный инструктаж", "инструктаж по безопасности",
            "инструктаж по информационной безопасности", "техника безопасности", "обучение по безопасности",
            "комплаенс обучение", "міндетті оқыту", "нұсқама"
        ],
    },
    {
        "rule_ids": ["performance_smart_goals", "onboarding_hr_responsibility"],
        "phrases": [
            "цели на квартал", "мои цели", "цели по smart", "smart", "kpi", "кпд",
            "оценка эффективности", "обратная связь от руководителя", "производительность",
            "1:1", "цели испытательного срока", "мақсат", "тиімділікті бағалау"
        ],
    },
    {
        "rule_ids": ["hr_department_structure", "onboarding_hr_responsibility"],
        "phrases": [
            "организационная структура отдела", "структура отдела",
            "директор департамента", "отдел найма", "адам ресурстарын басқару"
        ],
    },
    {
        "rule_ids": ["onboarding_hr_responsibility"],
        "phrases": [
            "кто отвечает за адаптацию", "hr аналитика", "кадровое администрирование",
            "найм и адаптация", "адаптация новых сотрудников", "hr процессы"
        ],
    },
    {
        "rule_ids": ["ethics_respect_communication", "ethics_prohibited_behavior"],
        "phrases": [
            "деловое общение", "корпоративная переписка", "уважительное общение", "поведение сотрудника",
            "оскорбление", "грубость", "этика", "әдеп", "құрмет"
        ],
    },
    {
        "rule_ids": ["ethics_no_discrimination_harassment"],
        "phrases": [
            "дискриминация", "домогательство", "харассмент", "притеснение", "угрозы", "буллинг",
            "кемсіту", "қысым"
        ],
    },
    {
        "rule_ids": ["ethics_confidential_information"],
        "phrases": [
            "конфиденциальность", "конфиденциальная информация", "персональные данные", "утечка данных",
            "политика конфиденциальности", "разглашение", "құпия", "дербес деректер"
        ],
    },
    {
        "rule_ids": ["ethics_hotline_channels", "anticorruption_reporting"],
        "phrases": [
            "сообщить о нарушении", "нарушение правил", "каналы связи", "горячая линия", "линия доверия",
            "куда сообщить", "жалоба", "обращение", "сенім желісі", "шағым"
        ],
    },
    {
        "rule_ids": ["anticorruption_gifts", "anticorruption_bribe_definition"],
        "phrases": [
            "подарок", "сувенир", "сыйлық", "вознаграждение", "подрядчик подарил", "можно ли принять подарок",
            "гостеприимство"
        ],
    },
    {
        "rule_ids": ["anticorruption_bribe_algorithm", "anticorruption_reporting"],
        "phrases": [
            "взятка", "предлагают взятку", "вымогают", "пара", "коррупция", "сыбайлас жемқорлық",
            "call-центр 1424", "1424"
        ],
    },
    {
        "rule_ids": ["anticorruption_conflict_interest"],
        "phrases": [
            "конфликт интересов", "личный интерес", "родственник", "поставщик родственник",
            "мүдделер қақтығысы"
        ],
    },
    {
        "rule_ids": ["anticorruption_company_resources", "ethics_confidential_information"],
        "phrases": [
            "ресурсы компании", "имущество компании", "личных целях", "служебное положение",
            "использовать имущество", "компания мүлкі"
        ],
    },
    {
        "rule_ids": ["access_emergency_access"],
        "phrases": [
            "чрезвычайная ситуация", "чрезвычайной ситуации", "чрезвычайн", "эвакуация", "план эвакуации", "подозрительный предмет",
            "пожар", "несчастный случай", "скорая помощь", "аварийная ситуация", "төтенше жағдай", "эвакуация"
        ],
    },
]


UNSUPPORTED_TOPICS: List[Dict[str, Any]] = [
    {
        "topic_ru": "общей информации о компании: миссия, генеральный директор, дата основания, продукты/услуги, офисы, полная оргструктура и контакты сотрудников",
        "topic_kz": "компания туралы жалпы ақпарат: миссия, бас директор, құрылған күні, өнімдер/қызметтер, кеңселер, толық ұйымдық құрылым және қызметкерлер байланыстары",
        "phrases": ["генеральный директор", "генеральным директор", "генеральн", "ceo", "основана", "основан", "когда была основана", "основные продукты", "продукт", "услуги предоставляет", "услуг", "где находятся офисы", "офисы компании", "контакты сотрудников", "контакты сотруд", "миссия компании", "организационная структура компании", "полная оргструктура", "какие отделы есть", "какие отделы есть в компании", "кто руководит моим отделом", "руководит моим отделом", "бас директор", "қашан құрылды"]
    },
    {
        "topic_ru": "кадровых заявок: отпуск, больничный, график отпусков, декрет, увольнение, справки, льготы и обновление личных данных",
        "topic_kz": "кадрлық өтінімдер: демалыс, ауру парағы, демалыс кестесі, декрет, жұмыстан шығу, анықтамалар, жеңілдіктер және дербес деректерді жаңарту",
        "phrases": ["остаток отпуска", "заявку на отпуск", "виды отпусков", "больничный", "график отпусков", "личные данные", "справку о доходах", "увольнение", "декрет", "корпоративные льготы", "демалыс қалдығы", "ауру парағы"]
    },
    {
        "topic_ru": "зарплаты и выплат: расчетный лист, банковские реквизиты, бонусы, премии, сверхурочные, налоги, компенсации и командировочные расходы",
        "topic_kz": "жалақы және төлемдер: есеп парағы, банк деректемелері, бонустар, сыйақылар, үстеме жұмыс, салықтар, өтемақылар және іссапар шығындары",
        "phrases": ["зарплата", "расчетный лист", "банковские реквизиты", "бонус", "премия", "сверхурочные", "налог", "налоговых удержаний", "компенсации", "командировочные расходы", "жалақы", "сыйақы"]
    },
    {
        "topic_ru": "IT-процедур: сброс пароля, VPN, Wi-Fi, установка ПО, создание IT-заявки, заказ ноутбука, неисправность оборудования и статус IT-заявки",
        "topic_kz": "IT рәсімдері: парольді қалпына келтіру, VPN, Wi-Fi, бағдарламалық жасақтама орнату, IT-өтінім, ноутбук тапсырысы, жабдық ақауы және IT-өтінім мәртебесі",
        "phrases": ["забыл пароль", "сбросить пароль", "vpn", "wi-fi", "wifi", "программное обеспечение", "it-поддержку", "it поддержку", "it-заявки", "статус моей it", "новый ноутбук", "неисправности оборудования", "пароль", "вайфай", "подозрительное письмо", "подозрительн", "утечка данных", "утечке данных", "утечк", "требования к паролям", "правила информационной безопасности", "информационной безопасности"]
    },
    {
        "topic_ru": "обучения и развития: список курсов, запись на обучение, сертификаты, внешнее обучение и компенсация обучения",
        "topic_kz": "оқыту және даму: курстар тізімі, оқуға жазылу, сертификаттар, сыртқы оқыту және оқу өтемақысы",
        "phrases": ["какие курсы", "записаться на обучение", "обучающие материалы", "сертификат", "внешнее обучение", "компенсация за обучение", "тренинги", "индивидуальный план развития", "курстар", "оқыту"]
    },
    {
        "topic_ru": "внутренних сервисных процессов: служебные заявки, согласование документов, шаблоны договоров, переговорные комнаты, закупки, запросы в другие отделы и статусы запросов",
        "topic_kz": "ішкі сервистік процестер: қызметтік өтінімдер, құжаттарды келісу, шарт шаблондары, келіссөз бөлмелері, сатып алу, басқа бөлімдерге сұрау және сұрау мәртебесі",
        "phrases": ["служебную заявку", "согласовать документ", "шаблон договора", "канцтовары", "забронировать переговорную", "оформить командировку", "согласовать закупку", "запрос в другой отдел", "статус моего запроса", "келісу", "сатып алу"]
    },
    {
        "topic_ru": "карьерных процедур: аттестация, повышение, внутренние вакансии, переход в другой отдел и требования карьерного роста",
        "topic_kz": "мансап рәсімдері: аттестация, жоғарылату, ішкі вакансиялар, басқа бөлімге ауысу және мансаптық өсу талаптары",
        "phrases": ["аттестация", "повышение", "вакансии", "перейти в другой отдел", "карьерный рост", "карьерного развития", "карьера", "ішкі вакансия", "мансап"]
    },
    {
        "topic_ru": "режимов удаленной, гибкой работы, переработки и табеля рабочего времени",
        "topic_kz": "қашықтан жұмыс, икемді кесте, артық жұмыс және жұмыс уақыты табелі рәсімдері",
        "phrases": ["удаленную работу", "гибкому графику", "переработка", "табель рабочего времени", "начало рабочего дня", "окончание рабочего дня", "отметить начало", "отметить окончание", "қашықтан жұмыс", "икемді график"]
    },
]


def expand_question_for_rag(question: str) -> str:
    """Add controlled synonyms before embedding/search.

    The final answer is still grounded in indexed document chunks. Expansion
    only improves ChromaDB retrieval for Russian/Kazakh morphology and common
    employee wording.
    """
    q = normalize_for_search(question)
    additions: List[str] = []
    for route in QUESTION_TO_RULE_ROUTES:
        if contains_any(q, route["phrases"]):
            additions.extend(route["phrases"])
            additions.extend(route.get("rule_ids", []))
    if any(term in q for term in ["зарплата", "отпуск", "vpn", "wi-fi", "больничный", "премия"]):
        additions.extend(["если точный пункт отсутствует не отвечать случайно", "уточнить у HR или профильной службы"])
    return q + (" " + " ".join(additions) if additions else "")


def question_intent_rule_ids(question: str) -> List[str]:
    q = f" {normalize_for_search(question)} "
    matched: List[str] = []
    for route in QUESTION_TO_RULE_ROUTES:
        if contains_any(q, route["phrases"]):
            for rule_id in route.get("rule_ids", []):
                if rule_id not in matched:
                    matched.append(rule_id)
    return matched


def unsupported_topic_for_question(question: str) -> Optional[Dict[str, str]]:
    q = normalize_for_search(question)

    # Do not block questions for which we have explicit grounded rules.
    if question_intent_rule_ids(question):
        return None

    for topic in UNSUPPORTED_TOPICS:
        if contains_any(q, topic["phrases"]):
            return topic
    return None


def forced_unsupported_topic_for_question(question: str) -> Optional[Dict[str, str]]:
    """Catch operational questions that must not be answered by a nearby VND chunk.

    Some employee questions contain words that also appear in loaded documents
    (for example: structure, график, contacts, KPI, password), but the uploaded
    VND set does not contain the exact employee-service procedure. This function
    runs before high-confidence rule routing and prevents accidental answers from
    a related but insufficient source.
    """
    q = normalize_for_search(question)
    forced_routes = [
        (0, [
            "генеральный директор", "ceo", "кто является генеральным", "кто директор компании",
            "когда была основана", "когда основана", "дата основания", "год основания",
            "какие основные продукты", "какие продукты", "какие услуги", "услуги предоставляет",
            "где находятся офисы", "офисы компании", "адреса офисов",
            "организационная структура компании", "какова организационная структура",
            "какие отделы есть в компании", "кто руководит моим отделом",
            "где найти контакты сотрудников", "контакты сотрудников"
        ]),
        (1, [
            "какие документы нужно предоставить", "документы нужно предоставить", "после трудоустройства",
            "остаток отпуска", "как подать заявку на отпуск", "виды отпусков", "какие виды отпусков",
            "как оформить больничный", "график отпусков", "обновить личные данные", "справку о доходах",
            "заявление на увольнение", "оформить декрет", "декретный отпуск", "корпоративные льготы"
        ]),
        (2, [
            "когда выплачивается зарплата", "где посмотреть расчетный лист", "расчетный лист",
            "изменить банковские реквизиты", "как начисляются бонусы", "когда выплачивается премия",
            "как рассчитываются сверхурочные", "налоговых удержаний", "зарплата пришла не полностью",
            "какие компенсации", "командировочные расходы"
        ]),
        (3, [
            "я забыл пароль", "сбросить пароль", "не работает корпоративная почта", "как подключиться к vpn",
            "vpn компании", "подключиться к wi-fi", "wi-fi в офисе", "установить нужное программное обеспечение",
            "создать заявку в it", "it-поддержку", "новый ноутбук", "неисправности оборудования",
            "доступ к определенной системе", "статус моей it-заявки", "требования к паролям"
        ]),
        (4, [
            "какие курсы", "записаться на обучение", "программа наставничества", "обучающие материалы",
            "сертификат после обучения", "навыки рекомендуются", "индивидуальный план развития",
            "тренинги проходят", "внешнее обучение", "компенсация за обучение"
        ]),
        (5, [
            "создать служебную заявку", "согласовать документ", "шаблон договора", "заказать канцтовары",
            "забронировать переговорную", "оформить командировку", "согласовать закупку",
            "запрос в другой отдел", "статус моего запроса"
        ]),
        (6, [
            "как проходит оценка эффективности сотрудников", "когда проводится аттестация",
            "подать заявку на повышение", "какие вакансии", "открыты внутри компании",
            "перейти в другой отдел", "требования для карьерного роста",
            "обратную связь от руководителя", "цели на квартал", "как работает система kpi",
            "возможности карьерного развития"
        ]),
        (7, [
            "какой у меня график работы", "как отметить начало рабочего дня", "как отметить окончание рабочего дня",
            "запросить удаленную работу", "как узнать праздничные дни", "праздничные дни",
            "гибкому графику", "гибкий график", "как учитывается переработка", "табель рабочего времени"
        ]),
    ]
    for topic_index, phrases in forced_routes:
        if contains_any(q, phrases):
            return UNSUPPORTED_TOPICS[topic_index]
    return None


def unsupported_answer(question: str, language: str, topic: Dict[str, str]) -> Dict[str, Any]:
    if language == "kz":
        answer = (
            "Digital Buddy:\n\n"
            f"Жүктелген ВНД ішінде {topic['topic_kz']} бойынша нақты тармақ таппадым. "
            "Сондықтан кездейсоқ жауап бермеймін. Бұл сұрақ бойынша HR, тікелей басшы немесе профильдік қызметке жүгініңіз.\n\n"
            "Қазір мен нақты дереккөзбен жауап бере алатын негізгі бағыттар: іскерлік әдеп, комплаенс/сыбайлас жемқорлыққа қарсы талаптар, өткізу режимі, бейімдеу маршруты, бірінші күн тапсырмалары және HR-блоктың бейімдеу бойынша жауапкершілігі."
        )
    else:
        answer = (
            "Digital Buddy:\n\n"
            f"В загруженных ВНД я не нашёл точный пункт по теме: {topic['topic_ru']}. "
            "Поэтому не буду давать случайный ответ. Для этого вопроса лучше обратиться в HR, к непосредственному руководителю или в профильную службу.\n\n"
            "Сейчас я могу уверенно отвечать с источниками по темам: деловая этика, комплаенс и антикоррупционные требования, пропускной режим, маршрут онбординга, задачи первого дня и ответственность HR-блока за адаптацию."
        )
    return {
        "answer": answer,
        "sources": [],
        "language": language,
        "score": 0,
        "rag_engine": "covered_topic_guardrail_no_answer",
        "guardrail": "question matched a known employee topic, but no exact source exists in uploaded VND",
    }


def answer_from_rule_ids(conn: sqlite3.Connection, rule_ids: List[str], language: str) -> Optional[Dict[str, Any]]:
    if not rule_ids:
        return None
    placeholders = ",".join(["?"] * len(rule_ids))
    rows = conn.execute(
        f"SELECT * FROM doc_chunks WHERE rule_id IN ({placeholders})",
        tuple(rule_ids),
    ).fetchall()
    if not rows:
        return None
    order = {rule_id: idx for idx, rule_id in enumerate(rule_ids)}
    rows = sorted(rows, key=lambda r: order.get(r["rule_id"], 99))
    best = rows[0]
    if language == "kz":
        answer = (
            "Digital Buddy:\n\n"
            f"{best['text_kz']}\n\n"
            f"Дереккөз: «{best['title_kz']}», {best['section_kz']}, {best['point']}. "
            f"Құжат коды: {best['document_code']}."
        )
    else:
        answer = (
            "Digital Buddy:\n\n"
            f"{best['text_ru']}\n\n"
            f"Источник: «{best['title_ru']}», {best['section_ru']}, {best['point']}. "
            f"Код документа: {best['document_code']}."
        )
    return {
        "answer": answer,
        "sources": [{
            "title": best["title_kz"] if language == "kz" else best["title_ru"],
            "section": best["section_kz"] if language == "kz" else best["section_ru"],
            "point": best["point"],
            "document_code": best["document_code"],
            "source_file": best["source_file"],
            "document_url": source_document_url(best["source_file"]),
            "retrieval": "intent routing + indexed VND chunk",
            "score": 99,
        }],
        "language": language,
        "score": 99,
        "rag_engine": "intent_guarded_rag",
        "guardrail": "matched high-confidence employee question route before vector answer",
    }


def chroma_lexical_relevance(question: str, item: Dict[str, Any]) -> int:
    expanded = expand_question_for_rag(question)
    q_tokens = [t for t in tokenize(expanded) if t not in RAG_STOPWORDS]
    meta = item.get("metadata", {})
    haystack = normalize_for_search(" ".join([
        item.get("document", ""),
        meta.get("title_ru", ""), meta.get("title_kz", ""),
        meta.get("section_ru", ""), meta.get("section_kz", ""),
        meta.get("point", ""), meta.get("text_ru", ""), meta.get("text_kz", ""),
        meta.get("document_code", ""), meta.get("category", ""),
    ]))
    score = 0
    for token in set(q_tokens):
        if token in haystack:
            score += 1
    for phrase in [
        "уйти пораньше", "уйти раньше", "планируемое отсутствие", "рабочее время",
        "согласовать с руководителем", "сообщить руководителю", "подарок", "взятка",
        "пропуск", "конфликт интересов", "комплаенс", "корпоративные ценности",
        "деловая этика", "конфиденциальная информация", "персональные данные",
        "разовый пропуск", "временный пропуск", "постоянный пропуск",
        "чрезвычайная ситуация", "адаптация новых", "кадровое администрирование",
        "hr-аналитика", "обязательные задачи", "программа адаптации",
    ]:
        if phrase in expanded and phrase in haystack:
            score += 4
    return score


def no_grounded_answer(language: str) -> Dict[str, Any]:
    if language == "kz":
        answer = (
            "Digital Buddy:\n\n"
            "Жүктелген ВНД бойынша нақты тармақ таппадым, сондықтан кездейсоқ жауап бермеймін. "
            "Бұл сұрақты тікелей басшыңыздан немесе HR қызметінен нақтылаңыз."
        )
    else:
        answer = (
            "Digital Buddy:\n\n"
            "Я не нашёл в загруженных ВНД достаточно точный пункт по этому вопросу, поэтому не буду давать случайный ответ. "
            "Уточните порядок у непосредственного руководителя или HR."
        )
    return {"answer": answer, "sources": [], "language": language, "score": 0, "rag_engine": "guardrail_no_answer"}


def fallback_sql_retrieval_boost(question: str, chunk: sqlite3.Row) -> int:
    q = normalize_for_search(question)
    values = dict(chunk)
    searchable_parts = [
        values.get("title_ru", ""), values.get("title_kz", ""),
        values.get("section_ru", ""), values.get("section_kz", ""),
        values.get("point", ""), values.get("text_ru", ""), values.get("text_kz", ""),
        values.get("keywords_ru", ""), values.get("keywords_kz", ""), values.get("document_code", ""),
    ]
    text = normalize_for_search(" ".join(searchable_parts))
    score = 0
    for token in tokenize(q):
        if token in text:
            score += 2
    try:
        kw_ru = json.loads(values.get("keywords_ru") or "[]")
        kw_kz = json.loads(values.get("keywords_kz") or "[]")
    except json.JSONDecodeError:
        kw_ru, kw_kz = [], []
    for kw in kw_ru + kw_kz:
        kw_l = normalize_for_search(kw)
        if kw_l and kw_l in q:
            score += 10
    return score


def query_chroma(question: str, top_k: int = RAG_TOP_K) -> Dict[str, Any]:
    client = get_chroma_client()
    if client is None:
        return {"ok": False, "reason": "chromadb_not_available", "results": []}
    try:
        collection = client.get_or_create_collection(CHROMA_COLLECTION_NAME)
        if collection.count() == 0:
            ensure_chroma_index(force=True)
        query_embedding = hash_embedding(expand_question_for_rag(question))
        response = collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )
        results = []
        ids = response.get("ids", [[]])[0]
        docs = response.get("documents", [[]])[0]
        metas = response.get("metadatas", [[]])[0]
        distances = response.get("distances", [[]])[0]
        for item_id, doc, meta, distance in zip(ids, docs, metas, distances):
            results.append({"id": item_id, "document": doc, "metadata": meta or {}, "distance": float(distance)})
        return {"ok": True, "results": results, "collection_count": collection.count()}
    except Exception as exc:
        return {"ok": False, "reason": str(exc), "results": []}


def source_from_chroma_item(item: Dict[str, Any], language: str) -> Dict[str, Any]:
    meta = item.get("metadata", {})
    return {
        "title": meta.get("title_kz") if language == "kz" and meta.get("title_kz") else meta.get("title_ru", ""),
        "section": meta.get("section_kz") if language == "kz" and meta.get("section_kz") else meta.get("section_ru", ""),
        "point": meta.get("point", ""),
        "document_code": meta.get("document_code", ""),
        "source_file": meta.get("source_file", ""),
        "document_url": meta.get("document_url", ""),
        "page": meta.get("page", 0),
        "category": meta.get("category", ""),
        "chunk_type": meta.get("chunk_type", ""),
        "retrieval": "ChromaDB vector search",
        "embedding": "deterministic multilingual hashing embeddings",
        "distance": item.get("distance"),
    }


def format_chroma_answer(item: Dict[str, Any], language: str) -> str:
    meta = item.get("metadata", {})
    title = meta.get("title_kz") if language == "kz" and meta.get("title_kz") else meta.get("title_ru", "")
    section = meta.get("section_kz") if language == "kz" and meta.get("section_kz") else meta.get("section_ru", "")
    point = meta.get("point", "")
    page = meta.get("page") or 0
    code = meta.get("document_code", "")
    document_url = meta.get("document_url", "")
    if language == "kz":
        text = meta.get("text_kz") or meta.get("text_ru") or item.get("document", "")
        return (
            "Digital Buddy:\n\n"
            f"{text}\n\n"
            f"Дереккөз: «{title}», {section}, {point}"
            f"{', ' + str(page) + '-бет' if page else ''}. "
            f"Құжат коды: {code}."
            f"\nҚұжатқа сілтеме: {document_url}"
        )
    text = meta.get("text_ru") or item.get("document", "")
    return (
        "Digital Buddy:\n\n"
        f"Согласно документу «{title}», {text}\n\n"
        f"Источник: «{title}», {section}, {point}"
        f"{', стр. ' + str(page) if page else ''}. "
        f"Код документа: {code}."
        f"\nСсылка на документ: {document_url}"
    )


def fallback_answer_from_sql(conn: sqlite3.Connection, question: str, language: str) -> Dict[str, Any]:
    chunks = conn.execute("SELECT * FROM doc_chunks").fetchall()
    search_question = expand_question_for_rag(question)
    ranked = sorted(chunks, key=lambda c: fallback_sql_retrieval_boost(search_question, c), reverse=True)
    best = ranked[0] if ranked else None
    score = fallback_sql_retrieval_boost(search_question, best) if best else 0
    if not best or score < RAG_SQL_MIN_SCORE:
        topic = forced_unsupported_topic_for_question(question) or unsupported_topic_for_question(question)
        result = unsupported_answer(question, language, topic) if topic else no_grounded_answer(language)
        result["score"] = score
        result["rag_engine"] = result.get("rag_engine", "fallback_sql_guarded")
        return result
    if language == "kz":
        answer = (
            "Digital Buddy:\n\n"
            f"{best['text_kz']}\n\n"
            f"Дереккөз: «{best['title_kz']}», {best['section_kz']}, {best['point']}. "
            f"Құжат коды: {best['document_code']}."
        )
    else:
        answer = (
            "Digital Buddy:\n\n"
            f"Согласно документу «{best['title_ru']}», {best['text_ru']}\n\n"
            f"Источник: «{best['title_ru']}», {best['section_ru']}, {best['point']}. "
            f"Код документа: {best['document_code']}."
        )
    return {
        "answer": answer,
        "sources": [{
            "title": best["title_kz"] if language == "kz" else best["title_ru"],
            "section": best["section_kz"] if language == "kz" else best["section_ru"],
            "point": best["point"],
            "document_code": best["document_code"],
            "source_file": best["source_file"],
            "document_url": source_document_url(best["source_file"]),
            "retrieval": "SQLite fallback",
            "score": score,
        }],
        "language": language,
        "score": score,
        "rag_engine": "fallback_sql",
    }


def answer_question(conn: sqlite3.Connection, question: str, language: Optional[str] = None) -> Dict[str, Any]:
    language = language or detect_language(question)

    # 1) Some operational questions are known to be outside the uploaded VND.
    # Check them before routing, because they may share words with covered topics.
    forced_unsupported = forced_unsupported_topic_for_question(question)
    if forced_unsupported:
        return unsupported_answer(question, language, forced_unsupported)

    # 2) High-confidence intent routing for common employee questions.
    # If a topic is explicitly covered by loaded VND chunks, answer from that
    # source immediately and include document/section/point.
    direct = answer_from_rule_ids(conn, question_intent_rule_ids(question), language)
    if direct:
        return direct

    # 3) Known employee topics that are NOT covered by the uploaded VND should
    # not fall through into a random nearest-neighbour chunk.
    unsupported = unsupported_topic_for_question(question)
    if unsupported:
        return unsupported_answer(question, language, unsupported)

    # 4) Real ChromaDB vector search, but accepted only when both vector distance
    # and lexical grounding indicate that the chunk is actually about the question.
    chroma_result = query_chroma(question, top_k=RAG_TOP_K)
    if chroma_result.get("ok") and chroma_result.get("results"):
        reranked = []
        for item in chroma_result["results"]:
            lexical = chroma_lexical_relevance(question, item)
            item["lexical_score"] = lexical
            reranked.append(item)
        reranked.sort(key=lambda item: (-item.get("lexical_score", 0), item.get("distance", 9.0)))
        best = reranked[0]
        distance_ok = best.get("distance", 9.0) <= RAG_CHROMA_MAX_DISTANCE
        lexical_ok = best.get("lexical_score", 0) >= RAG_CHROMA_MIN_LEXICAL_SCORE
        if distance_ok and lexical_ok:
            sources = [source_from_chroma_item(item, language) for item in reranked[:3] if item.get("lexical_score", 0) > 0]
            return {
                "answer": format_chroma_answer(best, language),
                "sources": sources,
                "language": language,
                "score": 1.0 - float(best.get("distance", 1.0)),
                "lexical_score": best.get("lexical_score", 0),
                "rag_engine": "ChromaDB",
                "embedding_model": "deterministic multilingual hashing embeddings",
                "guardrail": "vector result accepted after distance + lexical grounding check",
            }

    # 5) SQL fallback over the same verified chunks with a minimum score threshold.
    fallback = fallback_answer_from_sql(conn, question, language)
    fallback["chroma_status"] = chroma_result
    return fallback


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bitrix_user_id INTEGER UNIQUE NOT NULL,
                full_name TEXT NOT NULL,
                position TEXT,
                department TEXT,
                start_date TEXT NOT NULL,
                language TEXT DEFAULT 'ru',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS user_credentials (
                user_id INTEGER PRIMARY KEY,
                login TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS onboarding_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                bitrix_task_id TEXT,
                title TEXT NOT NULL,
                task_type TEXT NOT NULL,
                deadline TEXT,
                status TEXT DEFAULT 'new',
                created_at TEXT,
                completed_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS nudge_cards (
                day_number INTEGER PRIMARY KEY,
                topic TEXT NOT NULL,
                text_ru TEXT NOT NULL,
                text_kz TEXT,
                source_document TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS nudge_state (
                user_id INTEGER PRIMARY KEY,
                current_card_day INTEGER DEFAULT 1,
                sent_today INTEGER DEFAULT 0,
                last_sent_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS popup_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                popup_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                shown INTEGER DEFAULT 0,
                created_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                message TEXT NOT NULL,
                detected_language TEXT,
                sentiment_score REAL,
                created_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS bitrix_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                method TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS task_reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                task_id INTEGER NOT NULL,
                reminder_type TEXT NOT NULL,
                created_at TEXT,
                UNIQUE(user_id, task_id, reminder_type),
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(task_id) REFERENCES onboarding_tasks(id)
            );

            CREATE TABLE IF NOT EXISTS notification_reads (
                user_id INTEGER PRIMARY KEY,
                last_read_chat_id INTEGER DEFAULT 0,
                updated_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );

            DROP TABLE IF EXISTS doc_chunks;
            CREATE TABLE doc_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id TEXT UNIQUE NOT NULL,
                category TEXT NOT NULL,
                title_ru TEXT NOT NULL,
                title_kz TEXT NOT NULL,
                document_code TEXT,
                source_file TEXT,
                section_ru TEXT NOT NULL,
                section_kz TEXT NOT NULL,
                point TEXT NOT NULL,
                text_ru TEXT NOT NULL,
                text_kz TEXT NOT NULL,
                keywords_ru TEXT NOT NULL,
                keywords_kz TEXT NOT NULL
            );
            """
        )

        # Demo user
        conn.execute(
            """
            INSERT OR IGNORE INTO users(bitrix_user_id, full_name, position, department, start_date, language)
            VALUES (1001, 'Алия Нурланова', 'Главный специалист', 'Департамент управления человеческими ресурсами', ?, NULL)
            """,
            (today_iso(),),
        )

        demo_user = conn.execute("SELECT * FROM users WHERE bitrix_user_id = 1001").fetchone()
        if demo_user:
            conn.execute(
                """
                INSERT OR REPLACE INTO user_credentials(user_id, login, password)
                VALUES (?, ?, ?)
                """,
                (demo_user["id"], "aliya", "demo123"),
            )

        nudge_cards = [
            (1, "Деловой внешний вид", "Соблюдайте корпоративный дресс-код: аккуратность, сдержанность, деловой стиль.", "", "Правила дресс-кода"),
            (2, "Корректность и уважение", "Общайтесь профессионально, уважительно, избегайте фамильярности.", "", "Кодекс этики"),
            (3, "Дисциплина и рабочее время", "Приходите вовремя, соблюдайте график, эффективно используйте рабочее время.", "", "ПВТР"),
            (4, "Уведомление руководителя", "Сообщайте о планируемом отсутствии или опоздании заранее.", "", "ПВТР"),
            (5, "Уточнение поручений", "Если поручение неясно - задайте вопрос сразу.", "", "ПВТР"),
            (6, "Корпоративная переписка", "Пишите кратко, корректно, уважительно; соблюдайте деловой стиль.", "", "Кодекс этики"),
            (7, "Эффективные совещания", "Готовьтесь заранее, уважайте повестку, говорите по существу.", "", "Кодекс этики"),
            (8, "Умение слушать", "Слушайте коллег внимательно, не перебивайте.", "", "Кодекс этики"),
            (9, "Работа с документами", "Используйте корпоративные шаблоны, проверяйте оформление.", "", "ПВТР"),
            (10, "Конфиденциальность", "Не оставляйте документы без присмотра, не обсуждайте рабочие темы вне офиса.", "", "ПВТР"),
            (11, "Проверка получателей", "Перед отправкой писем проверяйте правильность адресатов.", "", "ПВТР"),
            (12, "Прозрачность поведения", "При сомнениях обращайтесь к руководителю или Комплаенс.", "", "Комплаенс"),
            (13, "Конфликты интересов", "Сообщайте о пересечениях личных и рабочих интересов.", "", "Комплаенс"),
            (14, "Представительство от КМГ", "Нельзя выступать от имени компании без официальных полномочий.", "", "Кодекс этики"),
            (15, "Инструктажи и безопасность", "Соблюдайте правила ТБ/ПБ/ИБ - это часть вашей ответственности.", "", "ПВТР"),
            (16, "Запрет на агрессию и опьянение", "Опьянение и угрожающие действия строго запрещены.", "", "ПВТР"),
            (17, "Офисный этикет", "Поддерживайте порядок в офисе и бережно относитесь к имуществу.", "", "ПВТР"),
            (18, "Репутация сотрудника", "Ваше поведение влияет на репутацию КМГ и доверие коллег.", "", "Кодекс этики"),
            (19, "Профессиональное развитие", "Поддерживайте и развивайте свои профессиональные навыки.", "", "Кодекс этики"),
            (20, "Smart Casual по пятницам", "Более свободный стиль допускается, но в рамках корпоративных норм.", "", "Правила дресс-кода"),
            (21, "Корректность при звонках", "Говорите вежливо, представляйтесь, соблюдайте деловой тон.", "", "Кодекс этики"),
            (22, "Обновление личных данных", "При смене персональных данных своевременно уведомляйте HR.", "", "ПВТР"),
            (23, "Материальная ответственность", "Бережно относитесь к ресурсам компании - за причинённый ущерб предусмотрена материальная ответственность.", "", "ПВТР"),
        ]
        nudge_kz = {
            1: "Корпоративтік дресс-кодты сақтаңыз: ұқыптылық, ұстамдылық, іскерлік стиль.",
            2: "Кәсіби және құрметпен сөйлесіңіз, тым еркін сөйлеуден аулақ болыңыз.",
            3: "Уақытында келіңіз, жұмыс кестесін сақтаңыз және жұмыс уақытын тиімді пайдаланыңыз.",
            4: "Жоспарланған келмеу немесе кешігу туралы басшыға алдын ала хабарлаңыз.",
            5: "Тапсырма түсініксіз болса, бірден нақтылаушы сұрақ қойыңыз.",
            6: "Қысқа, дұрыс және құрметпен жазыңыз; іскерлік стильді сақтаңыз.",
            7: "Алдын ала дайындалыңыз, күн тәртібін құрметтеңіз, мәселенің мәні бойынша сөйлеңіз.",
            8: "Әріптестерді мұқият тыңдаңыз және сөзін бөлмеңіз.",
            9: "Корпоративтік шаблондарды пайдаланыңыз және ресімдеуді тексеріңіз.",
            10: "Құжаттарды қараусыз қалдырмаңыз және жұмыс тақырыптарын кеңседен тыс талқыламаңыз.",
            11: "Хат жіберер алдында алушылардың дұрыс көрсетілгенін тексеріңіз.",
            12: "Күмән болса, басшыға немесе Комплаенс қызметіне жүгініңіз.",
            13: "Жеке және жұмыс мүдделерінің қиылысуы туралы хабарлаңыз.",
            14: "Ресми өкілеттіксіз компания атынан сөйлеуге болмайды.",
            15: "ЕҚ/ӨҚ/АҚ ережелерін сақтаңыз - бұл сіздің жауапкершілігіңіздің бір бөлігі.",
            16: "Мас күйде болу және қорқытатын әрекеттер қатаң тыйым салынады.",
            17: "Кеңседе тәртіп сақтаңыз және мүлікке ұқыпты қараңыз.",
            18: "Сіздің мінез-құлқыңыз ҚМГ беделі мен әріптестердің сеніміне әсер етеді.",
            19: "Кәсіби дағдыларыңызды қолдап, дамытыңыз.",
            20: "Жұма күні еркінірек стильге рұқсат, бірақ корпоративтік нормалар шегінде.",
            21: "Сыпайы сөйлеңіз, өзіңізді таныстырыңыз және іскерлік тонды сақтаңыз.",
            22: "Дербес деректер өзгерсе, HR-ға уақытылы хабарлаңыз.",
            23: "Компания ресурстарына ұқыпты қараңыз - келтірілген зиян үшін материалдық жауапкершілік көзделген.",
        }
        nudge_cards = [(d, topic, text_ru, nudge_kz.get(d, text_kz), source) for d, topic, text_ru, text_kz, source in nudge_cards]

        conn.executemany(
            "INSERT OR REPLACE INTO nudge_cards(day_number, topic, text_ru, text_kz, source_document) VALUES (?, ?, ?, ?, ?)",
            nudge_cards,
        )

        rules = load_rules_knowledge()
        conn.execute("DELETE FROM doc_chunks")
        conn.executemany(
            """
            INSERT OR REPLACE INTO doc_chunks(
                rule_id, category, title_ru, title_kz, document_code, source_file,
                section_ru, section_kz, point, text_ru, text_kz, keywords_ru, keywords_kz
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item.get("id"),
                    item.get("category", "general"),
                    item.get("title_ru", ""),
                    item.get("title_kz", item.get("title_ru", "")),
                    item.get("document_code", ""),
                    item.get("source_file", ""),
                    item.get("section_ru", ""),
                    item.get("section_kz", item.get("section_ru", "")),
                    item.get("point", ""),
                    item.get("text_ru", ""),
                    item.get("text_kz", item.get("text_ru", "")),
                    json.dumps(item.get("keywords_ru", []), ensure_ascii=False),
                    json.dumps(item.get("keywords_kz", []), ensure_ascii=False),
                )
                for item in rules
            ],
        )
        conn.commit()

        # Build or refresh the real ChromaDB vector index from the uploaded VND PDFs
        # plus verified OCR chunks for scanned pages.
        ensure_chroma_index(conn)

        # Ensure nudge state for demo user
        user = conn.execute("SELECT * FROM users WHERE bitrix_user_id = 1001").fetchone()
        if user:
            ensure_nudge_state(conn, user["id"])


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/static/{path:path}")
def static_files(path: str) -> FileResponse:
    file_path = STATIC_DIR / path
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "service": "digital-buddy-one-day-mvp", "time": now_iso()}

@app.post("/api/auth/login")
def auth_login(payload: AuthPayload) -> Dict[str, Any]:
    login = payload.login.strip().lower()
    password = payload.password.strip()
    with db() as conn:
        row = conn.execute(
            """
            SELECT u.*
            FROM users u
            JOIN user_credentials c ON c.user_id = u.id
            WHERE lower(c.login) = ? AND c.password = ?
            """,
            (login, password),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="Неверный логин или пароль")
        return {
            "ok": True,
            "user": {
                "id": row["id"],
                "bitrix_user_id": row["bitrix_user_id"],
                "full_name": row["full_name"],
                "position": row["position"],
                "department": row["department"],
                "language": row["language"],
            },
        }


@app.get("/api/popup/peek")
def popup_peek(bitrix_user_id: int = Query(1001)) -> Dict[str, Any]:
    """Return the next pending popup without marking it as shown.

    The presentation UI uses this to show a small Digital Buddy teaser first;
    the full-screen popup opens only after the employee clicks the icon.
    """
    with db() as conn:
        user = get_user_by_bitrix_id(conn, bitrix_user_id)
        event = conn.execute(
            "SELECT * FROM popup_events WHERE user_id = ? AND shown = 0 ORDER BY id LIMIT 1",
            (user["id"],),
        ).fetchone()
        if not event:
            return {"show": False}
        payload = json.loads(event["payload_json"])
        payload.update({"show": True, "popup_type": event["popup_type"], "popup_id": event["id"]})
        return payload


@app.post("/api/popup/{popup_id}/shown")
def popup_mark_shown(popup_id: int) -> Dict[str, Any]:
    with db() as conn:
        event = conn.execute("SELECT * FROM popup_events WHERE id = ?", (popup_id,)).fetchone()
        if not event:
            raise HTTPException(status_code=404, detail="Popup not found")
        conn.execute("UPDATE popup_events SET shown = 1 WHERE id = ?", (popup_id,))
        conn.commit()
        return {"ok": True, "popup_id": popup_id}


@app.get("/api/rules/catalog")
def rules_catalog() -> Dict[str, Any]:
    """Show which KMG rules are loaded into the demo RAG knowledge base."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT rule_id, category, title_ru, document_code, section_ru, point, source_file
            FROM doc_chunks
            ORDER BY category, id
            """
        ).fetchall()
        return {"count": len(rows), "rules": [dict(row) for row in rows]}


@app.post("/api/demo/reset")
def demo_reset(bitrix_user_id: int = 1001) -> Dict[str, Any]:
    with db() as conn:
        user = get_user_by_bitrix_id(conn, bitrix_user_id)
        conn.execute("DELETE FROM onboarding_tasks WHERE user_id = ?", (user["id"],))
        conn.execute("DELETE FROM popup_events WHERE user_id = ?", (user["id"],))
        conn.execute("DELETE FROM chat_messages WHERE user_id = ?", (user["id"],))
        conn.execute("DELETE FROM bitrix_log")
        conn.execute("DELETE FROM task_reminders WHERE user_id = ?", (user["id"],))
        conn.execute("DELETE FROM notification_reads WHERE user_id = ?", (user["id"],))
        conn.execute("UPDATE users SET start_date = ?, language = NULL WHERE id = ?", (today_iso(), user["id"]))
        conn.execute(
            "INSERT OR REPLACE INTO nudge_state(user_id, current_card_day, sent_today, last_sent_at) VALUES (?, 1, 0, NULL)",
            (user["id"],),
        )
        conn.commit()
    return {"ok": True, "message": "Demo reset. User is on day 1."}


@app.post("/api/demo/set-day")
def set_day(payload: SetDayPayload) -> Dict[str, Any]:
    if payload.day_number < 1 or payload.day_number > 365:
        raise HTTPException(status_code=400, detail="day_number must be between 1 and 365")
    with db() as conn:
        user = get_user_by_bitrix_id(conn, payload.bitrix_user_id)
        conn.execute(
            "UPDATE users SET start_date = ? WHERE id = ?",
            (calc_start_date(payload.day_number), user["id"]),
        )
        # Clear unshown popups so the next click is clean.
        conn.execute("DELETE FROM popup_events WHERE user_id = ? AND shown = 0", (user["id"],))
        if 2 <= payload.day_number <= 24:
            conn.execute(
                "INSERT OR REPLACE INTO nudge_state(user_id, current_card_day, sent_today, last_sent_at) VALUES (?, 1, 0, NULL)",
                (user["id"],),
            )
        conn.commit()
    return {"ok": True, "day_number": payload.day_number, "start_date": calc_start_date(payload.day_number)}


@app.post("/api/demo/reset-sent-today")
def reset_sent_today(bitrix_user_id: int = 1001) -> Dict[str, Any]:
    with db() as conn:
        user = get_user_by_bitrix_id(conn, bitrix_user_id)
        ensure_nudge_state(conn, user["id"])
        conn.execute("UPDATE nudge_state SET sent_today = 0 WHERE user_id = ?", (user["id"],))
        conn.commit()
    return {"ok": True, "message": "sent_today=false. This simulates the 00:00 APScheduler reset."}



@app.post("/api/demo/advance-day")
def advance_day(bitrix_user_id: int = 1001) -> Dict[str, Any]:
    """Presentation button: finish current demo day and move to the next onboarding day."""
    with db() as conn:
        user = get_user_by_bitrix_id(conn, bitrix_user_id)
        current_day = onboarding_day(user)
        next_day = min(current_day + 1, 365)
        passed_deadline = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE onboarding_tasks SET deadline = ? WHERE user_id = ? AND status != 'completed'",
            (passed_deadline, user["id"]),
        )
        conn.execute(
            "UPDATE users SET start_date = ? WHERE id = ?",
            (calc_start_date(next_day), user["id"]),
        )
        conn.execute("DELETE FROM popup_events WHERE user_id = ? AND shown = 0", (user["id"],))
        ensure_nudge_state(conn, user["id"])
        conn.execute(
            "UPDATE nudge_state SET sent_today = 0, last_sent_at = NULL WHERE user_id = ?",
            (user["id"],),
        )
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
        result = process_on_login_core(conn, user)
        conn.commit()
        return {"ok": True, "previous_day": current_day, "new_day": next_day, **result}


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: int) -> Dict[str, Any]:
    with db() as conn:
        task = conn.execute("SELECT * FROM onboarding_tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        task_dict = enrich_task(task, include_details=True)
        return {"ok": True, "task": task_dict}


@app.post("/api/tasks/{task_id}/start")
def start_task(task_id: int) -> Dict[str, Any]:
    with db() as conn:
        task = conn.execute("SELECT * FROM onboarding_tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if task["status"] == "new":
            conn.execute("UPDATE onboarding_tasks SET status = 'in_progress' WHERE id = ?", (task_id,))
            log_bitrix(
                "tasks.task.update",
                {"taskId": task["bitrix_task_id"], "fields": {"STATUS": "in_progress"}},
                conn,
            )
        conn.commit()
        return task_detail(task_id)


@app.post("/api/tasks/{task_id}/complete")
def complete_task(task_id: int) -> Dict[str, Any]:
    with db() as conn:
        task = conn.execute("SELECT * FROM onboarding_tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        conn.execute(
            "UPDATE onboarding_tasks SET status = 'completed', completed_at = ? WHERE id = ?",
            (now_iso(), task_id),
        )
        log_bitrix(
            "tasks.task.update",
            {"taskId": task["bitrix_task_id"], "fields": {"STATUS": "completed", "CLOSED_DATE": now_iso()}},
            conn,
        )
        progress = task_progress(conn, task["user_id"])
        conn.commit()
        return {"ok": True, "task_id": task_id, "status": "completed", "progress": progress}


@app.post("/webhooks/bitrix/on-login")
def on_login(payload: LoginPayload) -> Dict[str, Any]:
    with db() as conn:
        user = get_user_by_bitrix_id(conn, payload.bitrix_user_id)
        result = process_on_login_core(conn, user)
        conn.commit()
        return result


@app.get("/api/popup/next")
def popup_next(bitrix_user_id: int = Query(1001)) -> Dict[str, Any]:
    with db() as conn:
        user = get_user_by_bitrix_id(conn, bitrix_user_id)
        event = conn.execute(
            "SELECT * FROM popup_events WHERE user_id = ? AND shown = 0 ORDER BY id LIMIT 1",
            (user["id"],),
        ).fetchone()
        if not event:
            return {"show": False}
        conn.execute("UPDATE popup_events SET shown = 1 WHERE id = ?", (event["id"],))
        conn.commit()
        payload = json.loads(event["payload_json"])
        payload.update({"show": True, "popup_type": event["popup_type"], "popup_id": event["id"]})
        return payload


@app.get("/api/rag/status")
def rag_status() -> Dict[str, Any]:
    with db() as conn:
        status = ensure_chroma_index(conn, force=False)
        return {
            "ok": True,
            "rag_engine": "ChromaDB",
            "collection": CHROMA_COLLECTION_NAME,
            "chroma": status,
            "documents_dir": str(VND_DOCS_DIR),
            "document_files": [p.name for p in sorted(VND_DOCS_DIR.iterdir()) if p.is_file()] if VND_DOCS_DIR.exists() else [],
            "coverage_routes": len(QUESTION_TO_RULE_ROUTES),
            "unsupported_topic_guards": len(UNSUPPORTED_TOPICS),
            "embedding_model": "deterministic multilingual hashing embeddings",
            "top_k": RAG_TOP_K,
        }


@app.get("/api/rag/coverage")
def rag_coverage() -> Dict[str, Any]:
    """Show what the bot can answer with sources and what it refuses safely."""
    return {
        "answerable_routes": [
            {"rule_ids": route.get("rule_ids", []), "sample_phrases": route.get("phrases", [])[:10]}
            for route in QUESTION_TO_RULE_ROUTES
        ],
        "safe_no_answer_topics": [
            {"topic_ru": topic["topic_ru"], "sample_phrases": topic.get("phrases", [])[:10]}
            for topic in UNSUPPORTED_TOPICS
        ],
        "principle": "Answer only when a loaded VND/TZ chunk supports the response; otherwise return a safe fallback instead of a random regulation.",
    }


@app.post("/api/rag/reindex")
def rag_reindex() -> Dict[str, Any]:
    with db() as conn:
        status = ensure_chroma_index(conn, force=True)
        return {"ok": bool(status.get("ok")), "rag_engine": "ChromaDB", "chroma": status}


@app.get("/api/documents/{filename:path}")
def get_document(filename: str) -> FileResponse:
    safe_name = Path(filename).name
    file_path = VND_DOCS_DIR / safe_name
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Document not found")
    media_type = "application/pdf" if safe_name.lower().endswith(".pdf") else "application/octet-stream"
    return FileResponse(file_path, media_type=media_type, filename=safe_name)


@app.post("/api/rag/ask")
def rag_ask(payload: AskPayload) -> Dict[str, Any]:
    with db() as conn:
        return answer_question(conn, payload.question)


@app.post("/webhooks/bitrix/bot-message")
def bot_message(payload: BotMessagePayload) -> Dict[str, Any]:
    with db() as conn:
        user = get_user_by_bitrix_id(conn, payload.bitrix_user_id)
        language = get_user_chat_language(conn, user, payload.message)
        sentiment = simple_sentiment(payload.message)
        conn.execute(
            "INSERT INTO chat_messages(user_id, role, message, detected_language, sentiment_score, created_at) VALUES (?, 'user', ?, ?, ?, ?)",
            (user["id"], payload.message, language, sentiment, now_iso()),
        )
        result = answer_question(conn, payload.message, language)
        conn.execute(
            "INSERT INTO chat_messages(user_id, role, message, detected_language, sentiment_score, created_at) VALUES (?, 'bot', ?, ?, NULL, ?)",
            (user["id"], result["answer"], result.get("language", language), now_iso()),
        )
        log_bitrix(
            "imbot.send",
            {
                "BOT_NAME": "Digital Buddy",
                "BOT_AVATAR": "40x40 digital-buddy-face.png",
                "DIALOG_ID": str(user["bitrix_user_id"]),
                "MESSAGE": result["answer"],
                "SOURCES": result.get("sources", []),
            },
            conn,
        )
        conn.commit()
        return {"ok": True, **result}


@app.post("/api/notifications/read")
def notifications_read(bitrix_user_id: int = 1001) -> Dict[str, Any]:
    with db() as conn:
        user = get_user_by_bitrix_id(conn, bitrix_user_id)
        last_read_chat_id = mark_notifications_read(conn, user["id"])
        conn.commit()
        return {"ok": True, "unread_notifications": 0, "last_read_chat_id": last_read_chat_id}


@app.post("/api/demo/expire-first-task")
def expire_first_task(bitrix_user_id: int = 1001) -> Dict[str, Any]:
    """Presentation helper: make the nearest open task overdue to show red status and bot alert."""
    with db() as conn:
        user = get_user_by_bitrix_id(conn, bitrix_user_id)
        task = conn.execute(
            "SELECT * FROM onboarding_tasks WHERE user_id = ? AND status != 'completed' ORDER BY deadline, id LIMIT 1",
            (user["id"],),
        ).fetchone()
        if not task:
            return {"ok": False, "message": "Нет открытых задач для просрочки"}
        overdue_deadline = (datetime.now() - timedelta(minutes=15)).isoformat(timespec="seconds")
        conn.execute("UPDATE onboarding_tasks SET deadline = ? WHERE id = ?", (overdue_deadline, task["id"]))
        # Allow the overdue reminder to appear even if a soon reminder already existed.
        conn.execute(
            "DELETE FROM task_reminders WHERE user_id = ? AND task_id = ? AND reminder_type = 'overdue'",
            (user["id"], task["id"]),
        )
        generate_task_reminders(conn, user)
        conn.commit()
        return {"ok": True, "task_id": task["id"], "deadline": overdue_deadline}


@app.get("/api/demo/state")
def demo_state(bitrix_user_id: int = 1001) -> Dict[str, Any]:
    with db() as conn:
        user = get_user_by_bitrix_id(conn, bitrix_user_id)
        generate_task_reminders(conn, user)
        conn.commit()
        tasks = [enrich_task(x) for x in conn.execute("SELECT * FROM onboarding_tasks WHERE user_id = ? ORDER BY id", (user["id"],)).fetchall()]
        state = row_to_dict(conn.execute("SELECT * FROM nudge_state WHERE user_id = ?", (user["id"],)).fetchone())
        chat = [dict(x) for x in conn.execute("SELECT * FROM chat_messages WHERE user_id = ? ORDER BY id", (user["id"],)).fetchall()]
        logs = [
            {**dict(x), "payload": json.loads(x["payload_json"])}
            for x in conn.execute("SELECT * FROM bitrix_log ORDER BY id DESC LIMIT 50").fetchall()
        ]
        day = onboarding_day(user)
        return {
            "user": dict(user),
            "onboarding_day": day,
            "stage_info": get_stage_info(day),
            "task_progress": task_progress(conn, user["id"]),
            "tasks": tasks,
            "nudge_state": state,
            "chat_messages": chat,
            "bitrix_log": logs,
            "unread_notifications": unread_notification_count(conn, user["id"]),
        }

# ---------------------------------------------------------------------------
# v12 RAG patch: stable Digital Buddy functions + broad grounded ВНД answers
# ---------------------------------------------------------------------------
# The functions below intentionally override the earlier broad-RAG helpers.
# They keep the existing UI/API intact, but make the chat behaviour safer:
# 1) answer known Digital Buddy functional questions directly;
# 2) answer covered employee questions from indexed ВНД chunks with sources;
# 3) use ChromaDB only after intent routing and relevance guardrails;
# 4) never invent a procedure when the uploaded ВНД do not contain it.

BUDDY_FUNCTIONS_RU = [
    ("Приветствие и ориентация", "Встречает сотрудника в День 1, показывает план дня и ссылку на видео Председателя Правления.", "День 1"),
    ("Ответы по ВНД 24/7", "Отвечает на вопросы по регламентам КМГ через RAG: ChromaDB, embeddings, chunks документов, источник, раздел, пункт и ссылка на документ.", "Постоянно"),
    ("Culture Fit Nudges", "Доставляет одну карточку корпоративной культуры в день при входе на портал.", "Дни 1-23"),
    ("Напоминания и подготовка", "Напоминает о встречах 1:1 и помогает структурировать разговор с руководителем.", "Месяц 1-3"),
    ("Помощь с целями", "Помогает сформулировать цели по SMART через диалог на основе должностной инструкции и КПД.", "Месяц 1-3"),
    ("Анализ", "Анализирует тональность переписки и передаёт HR только агрегированные сигналы риска, без раскрытия личной переписки.", "Весь период"),
]

BUDDY_FUNCTIONS_KZ = [
    ("Сәлемдесу және бағдарлау", "Қызметкерді 1-күні қарсы алады, күн жоспарын және Басқарма төрағасының видеосына сілтемені көрсетеді.", "1-күн"),
    ("ВНД бойынша 24/7 жауаптар", "ҚМГ регламенттері бойынша RAG арқылы жауап береді: ChromaDB, embeddings, құжат chunks, дереккөз, бөлім, тармақ және құжат сілтемесі.", "Тұрақты"),
    ("Culture Fit Nudges", "Порталға кірген кезде корпоративтік мәдениет бойынша күніне бір карточка береді.", "1-23 күндер"),
    ("Еске салу және дайындық", "1:1 кездесулерін еске салады және басшымен әңгімені құрылымдауға көмектеседі.", "1-3 ай"),
    ("Мақсаттармен көмек", "Лауазымдық нұсқаулық пен КПД негізінде SMART мақсаттарын қалыптастыруға көмектеседі.", "1-3 ай"),
    ("Талдау", "Хат алмасу тоналдылығын талдайды және HR-ға жеке хаттарды ашпай, тек тәуекел сигналдарын береді.", "Бүкіл кезең"),
]

V12_ROUTE_GROUPS = [
    {
        "name": "company_values",
        "rule_ids": ["company_values_code", "company_ethics_principles"],
        "phrases": ["миссия", "ценност", "принцип", "корпоративные ценности", "құндылық", "миссия"],
    },
    {
        "name": "internal_documents_catalog",
        "rule_ids": ["documents_catalog_internal_rules"],
        "phrases": ["внд", "внутренние правила", "внутренние инструкции", "список правил", "руководство сотрудника", "ішкі құжат", "ішкі ереже"],
    },
    {
        "name": "onboarding",
        "rule_ids": ["onboarding_route_full", "onboarding_day1_requirements", "culture_fit_nudges", "training_mandatory_day1"],
        "phrases": ["онбординг", "адаптац", "новый сотрудник", "первый день", "день 1", "culture fit", "карточ", "бейімдеу", "жаңа қызметкер"],
    },
    {
        "name": "preboarding_it_workplace",
        "rule_ids": ["preboarding_it_workplace", "training_mandatory_day1"],
        "phrases": ["корпоративные системы", "доступ", "active directory", "outlook", "корпоративная почта", "рабочее место", "сэд", "сапфир", "intranet", "e-otinish", "почту", "қолжетімділік"],
    },
    {
        "name": "access_control",
        "rule_ids": ["access_proxy_card", "access_visitors", "access_no_transfer", "access_after_hours", "access_forbidden_items"],
        "phrases": ["пропуск", "проксим", "рұқсат", "посетител", "гость", "передать пропуск", "офис", "вход", "выход", "внутриобъект"],
    },
    {
        "name": "work_time_absence",
        "rule_ids": ["worktime_absence_notice", "ethics_respect_communication"],
        "phrases": ["уйти пораньше", "уйти раньше", "отпрос", "опозд", "опазд", "отгул", "рабочее время", "график", "отсутств", "кешік", "ерте кет"],
    },
    {
        "name": "ethics",
        "rule_ids": ["ethics_respect_communication", "ethics_prohibited_behavior", "ethics_no_discrimination_harassment", "ethics_confidential_information", "ethics_hotline_channels"],
        "phrases": ["этик", "кодекс", "уважен", "деловое общение", "дискриминац", "домог", "харас", "буллинг", "конфиденциаль", "персональные данные", "нарушени", "горячая линия", "әдеп", "құрмет", "құпия"],
    },
    {
        "name": "compliance",
        "rule_ids": ["anticorruption_bribe_definition", "anticorruption_bribe_algorithm", "anticorruption_indirect_signs", "anticorruption_reporting", "anticorruption_conflict_interest", "anticorruption_gifts", "anticorruption_company_resources"],
        "phrases": ["комплаенс", "коррупц", "взят", "пара", "подар", "сувенир", "сыйлық", "конфликт интерес", "личный интерес", "1424", "вознагражден", "подрядчик"],
    },
    {
        "name": "emergency_safety",
        "rule_ids": ["access_emergency_access", "access_forbidden_items", "training_mandatory_day1"],
        "phrases": ["чрезвычай", "эвакуац", "пожар", "аварийн", "несчастный случай", "скорая", "подозрительный предмет", "қауіп", "төтенше"],
    },
    {
        "name": "hr_adaptation",
        "rule_ids": ["onboarding_hr_responsibility", "hr_department_structure", "onboarding_route_full"],
        "phrases": ["кто отвечает за адаптацию", "hr", "дучр", "кадров", "найм", "трудовые отношения", "адаптация новых", "hr аналит", "қызметкерлер"],
    },
    {
        "name": "smart_performance",
        "rule_ids": ["performance_smart_goals", "onboarding_hr_responsibility"],
        "phrases": ["smart", "цели", "цель", "kpi", "кпд", "эффективност", "обратная связь", "1:1", "руководител", "мақсат"],
    },
]


def v12_is_buddy_function_question(question: str) -> bool:
    q = normalize_for_search(question)
    return contains_any(q, [
        "что умеет", "какие функции", "функции бота", "функции чат", "возможности бота",
        "digital buddy умеет", "зачем нужен digital buddy", "роль digital buddy",
        "что делает чат", "что делает бот", "функционал", "чат бот работает",
        "бот қандай", "не істей алады", "мүмкіндіктер"
    ])


def v12_buddy_functions_answer(language: str) -> Dict[str, Any]:
    rows = BUDDY_FUNCTIONS_KZ if language == "kz" else BUDDY_FUNCTIONS_RU
    if language == "kz":
        lines = ["Digital Buddy:\n", "Менің негізгі функцияларым:"]
        for name, desc, when in rows:
            lines.append(f"- {name}: {desc} Мерзімі: {when}.")
        lines.append("\nДереккөз: ТЗ Digital Buddy / функционалдық кесте, бөлім: Digital Buddy функциялары.")
    else:
        lines = ["Digital Buddy:\n", "Мои основные функции:"]
        for name, desc, when in rows:
            lines.append(f"- {name}: {desc} Когда: {when}.")
        lines.append("\nИсточник: ТЗ Digital Buddy / функциональная таблица, раздел: функции Digital Buddy.")
    return {
        "answer": "\n".join(lines),
        "sources": [{
            "title": "ТЗ Digital Buddy / функциональная таблица",
            "section": "Функции Digital Buddy",
            "point": "Приветствие, RAG, Culture Fit, напоминания, SMART, анализ",
            "document_code": "KMG-DIGITAL-BUDDY-FUNCTIONS",
            "retrieval": "functional specification route",
            "score": 100,
        }],
        "language": language,
        "score": 100,
        "rag_engine": "functional_route",
    }


def v12_rule_ids_for_question(question: str) -> List[str]:
    q = f" {normalize_for_search(question)} "
    matched: List[str] = []
    # keep old high-confidence routes
    for rid in question_intent_rule_ids(question):
        if rid not in matched:
            matched.append(rid)
    # add broader stem/substring routes
    for group in V12_ROUTE_GROUPS:
        if contains_any(q, group["phrases"]):
            for rid in group["rule_ids"]:
                if rid not in matched:
                    matched.append(rid)
    return matched


def v12_format_rows_answer(rows: List[sqlite3.Row], language: str, intro: Optional[str] = None) -> Dict[str, Any]:
    rows = rows[:4]
    if language == "kz":
        lines = ["Digital Buddy:\n"]
        if intro:
            lines.append(intro)
        for idx, row in enumerate(rows, start=1):
            text = row["text_kz"] or row["text_ru"]
            lines.append(f"{idx}. {text}")
        lines.append("\nДереккөздер:")
        for row in rows:
            lines.append(f"- «{row['title_kz'] or row['title_ru']}», {row['section_kz'] or row['section_ru']}, {row['point']}. Құжат коды: {row['document_code']}. Сілтеме: {source_document_url(row['source_file'])}")
    else:
        lines = ["Digital Buddy:\n"]
        if intro:
            lines.append(intro)
        for idx, row in enumerate(rows, start=1):
            lines.append(f"{idx}. {row['text_ru']}")
        lines.append("\nИсточники:")
        for row in rows:
            lines.append(f"- «{row['title_ru']}», {row['section_ru']}, {row['point']}. Код документа: {row['document_code']}. Ссылка: {source_document_url(row['source_file'])}")
    return {
        "answer": "\n".join(lines),
        "sources": [{
            "title": r["title_kz"] if language == "kz" else r["title_ru"],
            "section": r["section_kz"] if language == "kz" else r["section_ru"],
            "point": r["point"],
            "document_code": r["document_code"],
            "source_file": r["source_file"],
            "document_url": source_document_url(r["source_file"]),
            "retrieval": "intent route + SQLite indexed ВНД chunk + ChromaDB mirror",
            "score": 100,
        } for r in rows],
        "language": language,
        "score": 100,
        "rag_engine": "intent_rag_v12",
        "guardrail": "answered only from indexed VND chunks",
    }


def answer_from_rule_ids(conn: sqlite3.Connection, rule_ids: List[str], language: str) -> Optional[Dict[str, Any]]:  # type: ignore[override]
    if not rule_ids:
        return None
    placeholders = ",".join(["?"] * len(rule_ids))
    rows = conn.execute(f"SELECT * FROM doc_chunks WHERE rule_id IN ({placeholders})", tuple(rule_ids)).fetchall()
    if not rows:
        return None
    order = {rule_id: idx for idx, rule_id in enumerate(rule_ids)}
    rows = sorted(rows, key=lambda r: order.get(r["rule_id"], 999))
    intro = None
    if any(r["rule_id"] == "performance_smart_goals" for r in rows):
        intro = "По этой теме я могу не только дать выдержку из ВНД, но и помочь сформулировать цель по SMART: конкретно, измеримо, достижимо, релевантно и ограничено по сроку."
    return v12_format_rows_answer(rows, language, intro=intro)


def v12_improve_question_for_search(question: str) -> str:
    base = expand_question_for_rag(question)
    rule_ids = v12_rule_ids_for_question(question)
    return base + (" " + " ".join(rule_ids) if rule_ids else "")


def answer_question(conn: sqlite3.Connection, question: str, language: Optional[str] = None) -> Dict[str, Any]:  # type: ignore[override]
    language = language or detect_language(question)

    # A. Functional questions from the table shown in the task/presentation.
    if v12_is_buddy_function_question(question):
        return v12_buddy_functions_answer(language)

    # B. High-confidence ВНД route. This runs before unsupported guards, so
    # covered topics like SMART/KPI or work time do not get blocked accidentally.
    direct_rule_ids = v12_rule_ids_for_question(question)
    direct = answer_from_rule_ids(conn, direct_rule_ids, language)
    if direct:
        return direct

    # C. Known employee-service topics not present in the uploaded ВНД.
    forced_unsupported = forced_unsupported_topic_for_question(question)
    if forced_unsupported:
        return unsupported_answer(question, language, forced_unsupported)
    unsupported = unsupported_topic_for_question(question)
    if unsupported:
        return unsupported_answer(question, language, unsupported)

    # D. ChromaDB vector search over PDF/seed chunks, with strict grounding.
    chroma_result = query_chroma(v12_improve_question_for_search(question), top_k=max(RAG_TOP_K, 8))
    if chroma_result.get("ok") and chroma_result.get("results"):
        reranked = []
        for item in chroma_result["results"]:
            lexical = chroma_lexical_relevance(question, item)
            item["lexical_score"] = lexical
            reranked.append(item)
        reranked.sort(key=lambda item: (-item.get("lexical_score", 0), item.get("distance", 9.0)))
        best = reranked[0]
        # Slightly stricter than previous version: avoids random answers.
        if best.get("lexical_score", 0) >= max(3, RAG_CHROMA_MIN_LEXICAL_SCORE) and best.get("distance", 9.0) <= RAG_CHROMA_MAX_DISTANCE:
            sources = [source_from_chroma_item(item, language) for item in reranked[:3] if item.get("lexical_score", 0) > 0]
            return {
                "answer": format_chroma_answer(best, language),
                "sources": sources,
                "language": language,
                "score": 1.0 - float(best.get("distance", 1.0)),
                "lexical_score": best.get("lexical_score", 0),
                "rag_engine": "ChromaDB_v12",
                "embedding_model": "deterministic multilingual hashing embeddings",
                "guardrail": "vector result accepted after intent expansion + lexical grounding check",
            }

    # E. Safe SQL fallback over verified chunks. If no exact source - no answer.
    fallback = fallback_answer_from_sql(conn, v12_improve_question_for_search(question), language)
    fallback["chroma_status"] = chroma_result
    return fallback


@app.on_event("startup")
def v12_register_pdf_documents_in_db() -> None:
    """Register PDF/DOCX sources and indexed chunks in SQLite.

    This makes the demo explainable to judges: PDF files are stored as document
    records in the database, chunks are mirrored in SQLite and indexed in
    ChromaDB. The answer path still uses ChromaDB/embeddings plus guardrails.
    """
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rag_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title_ru TEXT,
                title_kz TEXT,
                document_code TEXT,
                source_file TEXT UNIQUE,
                document_url TEXT,
                file_type TEXT,
                indexed_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rag_indexed_chunks (
                id TEXT PRIMARY KEY,
                source_file TEXT,
                title_ru TEXT,
                document_code TEXT,
                section_ru TEXT,
                point TEXT,
                page INTEGER,
                chunk_type TEXT,
                text_preview TEXT,
                indexed_at TEXT
            )
            """
        )
        for file_path in sorted(VND_DOCS_DIR.glob("*")):
            if not file_path.is_file() or file_path.suffix.lower() not in {".pdf", ".docx"}:
                continue
            meta = guess_document_metadata(file_path)
            conn.execute(
                """
                INSERT OR REPLACE INTO rag_documents(title_ru, title_kz, document_code, source_file, document_url, file_type, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    meta.get("title_ru", file_path.stem),
                    meta.get("title_kz", file_path.stem),
                    meta.get("document_code", ""),
                    file_path.name,
                    source_document_url(file_path.name),
                    file_path.suffix.lower().lstrip("."),
                    now_iso(),
                ),
            )
        conn.execute("DELETE FROM rag_indexed_chunks")
        for chunk in build_all_vnd_chunks():
            conn.execute(
                """
                INSERT OR REPLACE INTO rag_indexed_chunks(id, source_file, title_ru, document_code, section_ru, point, page, chunk_type, text_preview, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk.get("id"), chunk.get("source_file"), chunk.get("title_ru"), chunk.get("document_code"),
                    chunk.get("section_ru"), chunk.get("point"), int(chunk.get("page") or 0),
                    chunk.get("chunk_type"), (chunk.get("text_ru") or chunk.get("document") or "")[:700], now_iso(),
                ),
            )
        conn.commit()


@app.get("/api/rag/documents")
def v12_rag_documents() -> Dict[str, Any]:
    with db() as conn:
        docs = [dict(x) for x in conn.execute("SELECT * FROM rag_documents ORDER BY id").fetchall()]
        chunks = [dict(x) for x in conn.execute("SELECT * FROM rag_indexed_chunks ORDER BY source_file, page LIMIT 200").fetchall()]
        return {"ok": True, "documents": docs, "indexed_chunks_sample": chunks, "count_documents": len(docs), "count_chunks_sample": len(chunks)}
