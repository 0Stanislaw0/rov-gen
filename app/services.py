import json
import uuid
import urllib.parse
from datetime import datetime, timedelta
from jinja2 import Template
from email.message import EmailMessage
from email.policy import default
from email.utils import formatdate

from .models import HistoryItem, ReleaseData
from .utils import BASE_DIR, parse_date, parse_duration, get_safe_filename
from .config_loader import load_config
from .docx_gen import generate_docx

HISTORY_FILE = BASE_DIR / "history.json"
TEMPLATES_FILE = BASE_DIR / "templates.json"
CONFIG = load_config(str(BASE_DIR / "config.yaml"))

def load_history():
    """Загружает историю операций из JSON, преобразуя словари в модели Pydantic."""
    if not HISTORY_FILE.exists(): return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return [HistoryItem(**item) for item in json.load(f)]
    except Exception: return []

def load_templates():
    """Загружает шаблоны и фильтрует их, оставляя только те АС/ФП, которые есть в текущем конфиге."""
    if not TEMPLATES_FILE.exists(): return []
    try:
        with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
            templates = json.load(f)
        valid_mapping = CONFIG.get("fp_mapping", {})
        return [
            t for t in templates 
            if t.get("as_system") in valid_mapping 
            and t.get("fp_system") in valid_mapping.get(t.get("as_system"), [])
        ]
    except Exception: return []

def save_history(history_data):
    """Сериализует список моделей истории обратно в JSON файл."""
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump([item.model_dump() for item in history_data], f, ensure_ascii=False, indent=4, default=str)

def add_to_history(release_data, gen_name, disk_name, email_gen=None, email_disk=None):
    """Создает новую запись в истории и сохраняет её в начало списка (LIFO)."""
    history = load_history()
    new_item = HistoryItem(
        id=str(uuid.uuid4()),
        timestamp=datetime.now(),
        release_data=release_data,
        generated_filename=gen_name,
        disk_filename=disk_name,
        email_generated_filename=email_gen,
        email_disk_filename=email_disk
    )
    history.insert(0, new_item)
    save_history(history)
    return new_item

def prepare_release_context(data: ReleaseData):
    """Формирует контекст для шаблонов Word и Email, рассчитывая все временные интервалы."""
    # Проверка соответствия ФП выбранной АС согласно маппингу в конфиге
    valid_fps = CONFIG.get("fp_mapping", {}).get(data.as_system, [])
    if data.fp_system not in valid_fps:
        raise ValueError(f"Система {data.fp_system} не входит в состав {data.as_system}")

    system_meta = CONFIG.get("system_metadata", {}).get(data.as_system, {}).get(data.fp_system, {})
    start_dt = parse_date(data.start_date_time)
    
    # Формирование таблицы работ с расчетом времени завершения каждого этапа
    work_table = []
    t = start_dt
    for idx, stage in enumerate(data.stages, 1):
        dur = parse_duration(stage.duration_raw)
        end = t + timedelta(minutes=dur)
        work_table.append({
            "id": str(idx),
            "work": stage.description.strip(),
            "start_time": t.strftime('%d.%m.%y %H:%M'),
            "end_time": end.strftime('%d.%m.%y %H:%M'),
            "interval": f"{t.strftime('%d.%m.%y %H:%M')} - {end.strftime('%d.%m.%y %H:%M')}",
            "comment": stage.comment.strip()
        })
        t = end

    # Расчет принятия решения: начинается сразу после работ, округляется до ближайших 30 минут
    dp_start = t
    tmp_end = dp_start + timedelta(minutes=30)
    rem = tmp_end.minute % 30
    dp_end = tmp_end if rem == 0 else tmp_end + timedelta(minutes=(30 - rem))

    # План отката: время указывается относительно момента принятия решения (Y + HH:MM)
    rollback_table = []
    acc = 0
    for idx, stage in enumerate(data.rollback_stages, 1):
        dur = parse_duration(stage.duration_raw)
        s_rel = "Y" if acc == 0 else f"Y + {acc//60:02d}:{acc%60:02d}"
        acc += dur
        e_rel = f"Y + {acc//60:02d}:{acc%60:02d}"
        rollback_table.append({
            "id": str(idx),
            "work": stage.description.strip(),
            "start_time": s_rel,
            "end_time": e_rel,
            "interval": f"{s_rel} - {e_rel}",
            "comment": stage.comment.strip()
        })

    context = {
        "RELEASE_NUMBER": data.release_number,
        "START_DATE": start_dt.strftime('%d.%m.%y'),
        "START_TIME": start_dt.strftime('%H:%M'),
        "AS": data.as_system,
        "FP": data.fp_system,
        "RESPONSIBLE": data.responsible,
        "CONTACTS": data.contacts,
        "DESCRIPTION": data.description,
        "POSSIBLE_DOWNTIME": data.possible_downtime,
        "WORK_TABLE": work_table,
        "ROLLBACK_TABLE": rollback_table,
        "DP_START": dp_start.strftime('%H:%M'),
        "DP_END": dp_end.strftime('%H:%M'),
        "OWNER_NAME": system_meta.get("owner_name", ""),
        "BLOCK_VAL": system_meta.get("block_val", "")
    }
    return context, system_meta

def get_filenames(data: ReleaseData):
    """Генерирует пару имен: 'чистое' для пользователя и UUID-имя для хранения на диске."""
    safe_fp = get_safe_filename(data.fp_system)
    safe_rel = get_safe_filename(data.release_number)
    base = f"План_{safe_fp}_{safe_rel}"
    # Сохраняем файлы в подпапках generated/docs и generated/emls
    disk_docx = f"generated/docs/{uuid.uuid4().hex}.docx"
    disk_eml = f"generated/emls/{uuid.uuid4().hex}.eml"
    return base + ".docx", disk_docx, base + ".eml", disk_eml

def create_eml_content(data, system_meta, context):
    """Создает EML сообщение на основе HTML шаблона с вложением логотипа через Content-ID."""
    email_tpl_path = BASE_DIR / "templates" / "email_template.html"
    tpl_str = email_tpl_path.read_text(encoding="utf-8") if email_tpl_path.exists() else "<html><body>План работ</body></html>"
    html_content = Template(tpl_str).render(context)

    msg = EmailMessage(policy=default.clone(max_line_length=1000000))
    msg['Subject'] = f"Работы: {data.as_system} / {data.fp_system}"
    msg['From'] = f"{data.responsible} <no-reply@example.com>"
    msg['Date'] = formatdate(localtime=True)

    # Проверка: email_to может быть строкой, списком или отсутствовать (None)
    to_list = system_meta.get("email_to") or []
    if isinstance(to_list, str):
        to_list = [to_list]
    msg['To'] = ", ".join(to_list)

    msg.set_content("HTML content required")
    msg.add_alternative(html_content, subtype='html')
    
    # Вставка лого если есть
    static_img = BASE_DIR / "app" / "static" / "img" / "logo.jpg"
    if static_img.exists() and 'cid:logo' in html_content:
        # Безопасный поиск HTML-части
        for part in msg.iter_parts():
            if part.get_content_subtype() == 'html':
                part.add_related(static_img.read_bytes(), maintype="image", subtype="jpeg", cid='logo')
                break
    
    return msg