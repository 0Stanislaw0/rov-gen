import yaml
import re
import urllib.parse
import traceback
import uuid
import json
from datetime import datetime, timedelta 
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from email.message import EmailMessage
from email.policy import default
from email.utils import formatdate
from pydantic import BaseModel
from starlette.background import BackgroundTask # Оставляем импорт, но убираем использование в /generate
from typing import List, Optional
from pathlib import Path
from jinja2 import Template

from .docx_gen import generate_docx
from .config_loader import load_config

app = FastAPI()

# Определяем корень проекта (на уровень выше папки app)
BASE_DIR = Path(__file__).resolve().parent.parent

# Load Config
CONFIG = load_config(str(BASE_DIR / "config.yaml"))

class Stage(BaseModel):
    description: str
    duration_raw: str
    comment: str = ""

class ReleaseData(BaseModel):
    release_number: str
    start_date_time: str # Format: "dd.mm.yy HH:MM"
    as_system: str
    fp_system: str
    responsible: str
    contacts: str
    description: str
    possible_downtime: str
    stages: List[Stage]
    rollback_stages: List[Stage]

# Определяем путь к файлу истории
HISTORY_FILE = BASE_DIR / "history.json"
TEMPLATES_FILE = BASE_DIR / "templates.json"

# Новая Pydantic модель для элемента истории
class HistoryItem(BaseModel):
    id: str
    timestamp: datetime
    release_data: ReleaseData # Полные данные, использованные для генерации
    generated_filename: str # Имя файла, которое видит пользователь (например, "РОВ_АС_ФП_Релиз.docx")
    disk_filename: str # Уникальное имя файла на диске (например, "a1b2c3d4e5f6.docx")
    email_generated_filename: Optional[str] = None
    email_disk_filename: Optional[str] = None

# Вспомогательные функции для работы с историей
def load_history() -> List[HistoryItem]:
    """Загружает историю из JSON файла."""
    if not HISTORY_FILE.exists():
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            raw_history = json.load(f)
            # Преобразуем сырые словари в Pydantic модели HistoryItem
            return [HistoryItem(**item) for item in raw_history]
    except json.JSONDecodeError:
        print(f"WARNING: History file {HISTORY_FILE} is corrupted or empty. Starting with empty history.")
        return []

def save_history(history_data: List[HistoryItem]):
    """Сохраняет историю в JSON файл."""
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        # Преобразуем Pydantic модели в словари для JSON сериализации
        json.dump([item.dict() for item in history_data], f, ensure_ascii=False, indent=4, default=str)

def add_to_history(release_data: ReleaseData, generated_filename: str, disk_filename: str, email_gen: str = None, email_disk: str = None):
    """Добавляет новую запись в историю."""
    history = load_history()
    new_item = HistoryItem(
        id=str(uuid.uuid4()),
        timestamp=datetime.now(),
        release_data=release_data,
        generated_filename=generated_filename,
        disk_filename=disk_filename,
        email_generated_filename=email_gen,
        email_disk_filename=email_disk
    )
    history.insert(0, new_item) # Добавляем в начало для хронологического порядка
    save_history(history)
    return new_item.id

def parse_duration(raw: str) -> int:
    """Converts '1 hour', '60 min' etc to minutes."""
    raw = raw.lower()
    digits = re.findall(r'\d+', raw)
    if not digits:
        return 0
    val = int(digits[0])
    if 'час' in raw or 'hour' in raw:
        return val * 60
    return val

def parse_date(date_str: str) -> datetime:
    """Parses various date formats: 18.05.26, 18.05.2026, 18/05/26 etc."""
    clean_date = re.sub(r'[/-]', '.', date_str)
    # Пробуем разные форматы года (2 и 4 цифры)
    for fmt in ("%d.%m.%y %H:%M", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(clean_date, fmt)
        except ValueError:
            continue
    raise ValueError(f"Некорректный формат даты: {date_str}. Ожидается DD.MM.YY HH:MM или DD.MM.YYYY HH:MM")

@app.get("/config")
def get_config():
    # Создаем копию конфига, чтобы не мутировать глобальный объект
    res_config = CONFIG.copy()
    # Если в конфиге нет явного списка систем, берем ключи из fp_mapping
    if "as_list" not in res_config and "fp_mapping" in res_config:
        res_config["as_list"] = list(res_config["fp_mapping"].keys())
    # Добавляем глобальные дефолты для пользователя
    res_config.setdefault("default_responsible", "Иванов И.И.")
    res_config.setdefault("default_contacts", "+7-999-000-00-00")
    res_config.setdefault("as_start_times", {})
    return res_config

# Новый эндпоинт для получения списка истории
@app.get("/history")
def get_history_list():
    history = load_history()
    # Возвращаем упрощенный вид для списка, чтобы не передавать все данные формы
    return [
        {
            "id": item.id,
            "timestamp": item.timestamp.isoformat(),
            "release_number": item.release_data.release_number,
            "as_system": item.release_data.as_system,
            "fp_system": item.release_data.fp_system,
            "generated_filename": item.generated_filename,
            "disk_filename": item.disk_filename,
            "email_generated_filename": item.email_generated_filename,
            "email_disk_filename": item.email_disk_filename
        }
        for item in history
    ]

# Новый эндпоинт для получения полных данных конкретной записи истории
@app.get("/history/{item_id}")
def get_history_item_data(item_id: str):
    history = load_history()
    for item in history:
        if item.id == item_id:
            return item.release_data # Возвращаем полные данные ReleaseData для предзаполнения формы
    raise HTTPException(status_code=404, detail="Запись истории не найдена")

@app.get("/templates")
def get_templates():
    """Загружает список шаблонов из файла templates.json."""
    if not TEMPLATES_FILE.exists():
        return []
    try:
        with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
            templates = json.load(f)
        
        # Фильтруем шаблоны: показываем только те, что соответствуют текущему конфигу АС/ФП
        valid_mapping = CONFIG.get("fp_mapping", {})
        return [
            t for t in templates 
            if t.get("as_system") in valid_mapping 
            and t.get("fp_system") in valid_mapping[t.get("as_system")]
        ]
    except Exception:
        return []

@app.delete("/history/{item_id}")
def delete_history_item(item_id: str):
    history = load_history()
    item_to_delete = None
    
    # Ищем элемент
    for item in history:
        if item.id == item_id:
            item_to_delete = item
            break
    
    if not item_to_delete:
        raise HTTPException(status_code=404, detail="Запись не найдена")

    # Удаляем файл с диска
    file_path = BASE_DIR / item_to_delete.disk_filename
    if file_path.exists():
        file_path.unlink()

    if item_to_delete.email_disk_filename:
        email_path = BASE_DIR / item_to_delete.email_disk_filename
        if email_path.exists():
            email_path.unlink()

    # Сохраняем историю без этого элемента
    new_history = [i for i in history if i.id != item_id]
    save_history(new_history)
    return {"status": "success", "message": f"Запись {item_id} и файл удалены"}

def prepare_release_context(data: ReleaseData):
    """Общая логика подготовки данных для шаблона и письма."""
    # Валидация FP
    valid_fps = CONFIG.get("fp_mapping", {}).get(data.as_system, [])
    if data.fp_system not in valid_fps:
        raise ValueError(f"Система {data.fp_system} не входит в состав {data.as_system}")

    # Извлекаем дополнительные метаданные из конфига
    system_meta = CONFIG.get("system_metadata", {}).get(data.as_system, {}).get(data.fp_system, {})
    
    start_dt = parse_date(data.start_date_time)
    current_time = start_dt
    
    def process_stages(stages_list, start_from):
        table = []
        t = start_from
        for idx, stage in enumerate(stages_list, 1):
            duration_min = parse_duration(stage.duration_raw)
            end_time = t + timedelta(minutes=duration_min)
            s_str = t.strftime('%d.%m.%y %H:%M')
            e_str = end_time.strftime('%d.%m.%y %H:%M')
            table.append({
                "id": str(idx),
                "work": stage.description.strip(),
                "start_time": s_str,
                "end_time": e_str,
                "interval": f"{s_str} - {e_str}",
                "comment": stage.comment.strip() if stage.comment else ""
            })
            t = end_time
        return table, t

    work_table, current_time = process_stages(data.stages, start_dt)

    dp_start = current_time
    tmp_end = dp_start + timedelta(minutes=30)
    rem = tmp_end.minute % 30
    dp_end = tmp_end if rem == 0 else tmp_end + timedelta(minutes=(30 - rem))
    dp_end = dp_end.replace(second=0, microsecond=0)

    def format_relative(minutes):
        if minutes == 0: return "Y"
        h, m = divmod(minutes, 60)
        return f"Y + {h:02d}:{m:02d}"

    rollback_table = []
    accumulated_mins = 0
    for idx, stage in enumerate(data.rollback_stages, 1):
        dur = parse_duration(stage.duration_raw)
        start_rel = format_relative(accumulated_mins)
        accumulated_mins += dur
        end_rel = format_relative(accumulated_mins)
        rollback_table.append({
            "id": str(idx),
            "work": stage.description.strip(),
            "start_time": start_rel,
            "end_time": end_rel,
            "interval": f"{start_rel} - {end_rel}",
            "comment": stage.comment.strip() if stage.comment else ""
        })

    context = {
        "RELEASE_NUMBER": str(data.release_number).strip(),
        "START_DATE": start_dt.strftime('%d.%m.%y'),
        "START_TIME": start_dt.strftime('%H:%M'),
        "AS": str(data.as_system).strip(),
        "FP": str(data.fp_system).strip(),
        "RESPONSIBLE": str(data.responsible).strip(),
        "CONTACTS": str(data.contacts).strip(),
        "DESCRIPTION": str(data.description).strip(),
        "POSSIBLE_DOWNTIME": str(data.possible_downtime).strip(),
        "WORK_TABLE": work_table,
        "ROLLBACK_TABLE": rollback_table,
        "DP_START": dp_start.strftime('%H:%M'),
        "DP_END": dp_end.strftime('%H:%M'),
        "APPROVER_NAME": system_meta.get("approver_name", ""),
        "APPROVER_TITLE": system_meta.get("approver_title", ""),
        "OWNER_NAME": system_meta.get("owner_name", ""),
        "OWNER_CONTACTS": system_meta.get("owner_contacts", ""),
        "EMAIL_TO": (", ".join(system_meta.get("email_to")) if isinstance(system_meta.get("email_to"), list) else (system_meta.get("email_to") or "")),
        "BLOCK_VAL": system_meta.get("block_val", "")
    }
    return context, system_meta

def get_safe_filenames(data: ReleaseData):
    safe_fp = re.sub(r'[\\/*?:"<>|]', "_", data.fp_system)
    safe_release = re.sub(r'[\\/*?:"<>|]', "_", data.release_number)
    base_name = f"План_{safe_fp}_{safe_release}"
    return (
        f"{base_name}.docx", 
        f"{uuid.uuid4().hex}.docx",
        f"Письмо_{safe_fp}_{safe_release}.eml",
        f"{uuid.uuid4().hex}.eml"
    )

def find_template():
    """Поиск шаблона template.docx в возможных папках."""
    possible_paths = [BASE_DIR / "templates" / "template.docx", BASE_DIR / "app" / "templates" / "template.docx"]
    return next((p for p in possible_paths if p.exists()), None)

def create_eml_content(data: ReleaseData, system_meta: dict, context: dict):
    """Создает объект EmailMessage."""
    to_list = system_meta.get("email_to") or []
    if isinstance(to_list, str): to_list = [to_list]
    cc_list = system_meta.get("email_recipients") or []
    if isinstance(cc_list, str): cc_list = [cc_list]

    # Путь к HTML шаблону (можно вынести в config.yaml)
    email_tpl_path = BASE_DIR / "templates" / "email_template.html"
    
    if email_tpl_path.exists():
        with open(email_tpl_path, "r", encoding="utf-8") as f:
            template_str = f.read()
    else:
        # Фолбэк на простой текст, если файл не найден
        template_str = "<html><body><p>План работ для {{ FP }}.</p></body></html>"
    
    html_content = Template(template_str).render(context)

    # Используем большое значение вместо 0, чтобы избежать ValueError в Python 3.14
    # и при этом предотвратить нежелательные разрывы строк и знаки "="
    custom_policy = default.clone(max_line_length=1000000)
    msg = EmailMessage(policy=custom_policy)
    msg['Subject'] = f"Запланированные работы на: {data.as_system} / {data.fp_system} - {data.start_date_time}"
    msg['From'] = f"{data.responsible} <no-reply@example.com>"
    msg['To'] = ", ".join(to_list)
    msg['Cc'] = ", ".join(cc_list)
    msg['Date'] = formatdate(localtime=True)

    # Основная текстовая часть (для клиентов, не умеющих в HTML)
    msg.set_content(f"Запланированные работы на {data.as_system}.{data.fp_system} {data.start_date_time}")
    # HTML часть
    msg.add_alternative(html_content, subtype='html')
    
    # Находим созданную HTML-часть вручную для совместимости со всеми версиями Python
    html_part = next((part for part in msg.iter_parts() if part.get_content_subtype() == 'html'), None)

    # Логика вставки картинок (CID)
    # Если в шаблоне есть <img src="cid:logo">, прикрепляем файл
    static_img_path = BASE_DIR / "app" / "static" / "img" / "logo.jpg"
    if html_part and static_img_path.exists() and 'cid:logo' in html_content:
        with open(static_img_path, 'rb') as img:
            # Для JPEG формат в MIME строго "jpeg"
            html_part.add_related(
                img.read(),
                maintype="image",
                subtype="jpeg",
                cid='logo'
            )
    
    return msg

@app.post("/generate")
def generate_report(data: ReleaseData):
    try:
        context, system_meta = prepare_release_context(data)
        clean_docx, disk_docx, clean_eml, disk_eml = get_safe_filenames(data)
        file_path = BASE_DIR / disk_docx
        eml_path = BASE_DIR / disk_eml

        # Поиск шаблона
        template_path = find_template()
        if not template_path:
            raise FileNotFoundError("Шаблон template.docx не найден")

        generate_docx(str(template_path), str(file_path), context)
        
        # Генерируем EML сразу
        msg = create_eml_content(data, system_meta, context)
        with open(eml_path, 'wb') as f:
            f.write(msg.as_bytes())

        # Сохраняем в историю ссылки на оба файла
        add_to_history(data, clean_docx, disk_docx, clean_eml, disk_eml)

        encoded_name = urllib.parse.quote(clean_docx)
        return FileResponse(
            path=str(file_path),
            media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document', filename=clean_docx
        )
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/generate-email")
def generate_email(data: ReleaseData):
    # Оставляем метод для прямой генерации, если нужно
    context, system_meta = prepare_release_context(data)
    _, _, clean_eml, _ = get_safe_filenames(data)

    msg = create_eml_content(data, system_meta, context)
    temp_eml = BASE_DIR / f"temp_{uuid.uuid4().hex}.eml"
    with open(temp_eml, 'wb') as f: f.write(msg.as_bytes())
    
    return FileResponse(path=str(temp_eml), media_type='message/rfc822', filename=clean_eml, background=BackgroundTask(lambda: temp_eml.unlink(missing_ok=True)))

@app.get("/download/{filename}")
async def download_file(filename: str, pretty_name: str = None):
    decoded_filename = urllib.parse.unquote(filename)
    if ".." in decoded_filename or not (decoded_filename.endswith(".docx") or decoded_filename.endswith(".eml")):
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    file_path = BASE_DIR / decoded_filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Файл не найден")

    media_type = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' if decoded_filename.endswith(".docx") else 'message/rfc822'
    
    # Используем pretty_name для заголовка Content-Disposition, если оно передано
    display_name = pretty_name if pretty_name else decoded_filename # pretty_name уже декодирован
    return FileResponse(path=str(file_path), media_type=media_type, filename=display_name)

static_dir = BASE_DIR / "app" / "static"

@app.get("/")
async def read_index():
    return FileResponse(static_dir / "index.html")

app.mount("/static", StaticFiles(directory=static_dir), name="static")
