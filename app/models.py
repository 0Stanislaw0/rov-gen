from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime

class Stage(BaseModel):
    """Этап работ или отката."""
    description: str
    duration_raw: str
    comment: str = ""

class ReleaseData(BaseModel):
    """Набор данных формы генерации распоряжения."""
    release_number: str
    start_date_time: str  # Format: "dd.mm.yy HH:MM"
    as_system: str
    fp_system: str
    responsible: str
    contacts: str
    description: str
    possible_downtime: str
    stages: List[Stage]
    rollback_stages: List[Stage]
    docx_template: Optional[str] = "template.docx"

class HistoryItem(BaseModel):
    """Запись в журнале истории генераций."""
    id: str
    timestamp: datetime
    release_data: ReleaseData
    generated_filename: str
    disk_filename: str
    email_generated_filename: Optional[str] = None
    email_disk_filename: Optional[str] = None