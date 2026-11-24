# models.py - Типизированные модели предметной области (без мёртвого ORM-дублирования)
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional
from werkzeug.security import generate_password_hash, check_password_hash


def _utc_now():
    return datetime.now(timezone.utc)


@dataclass
class User:
    """Модель пользователя информационной системы."""
    username: Optional[str] = None
    password_hash: Optional[str] = None
    user_type: str = 'farm'
    farm_name: Optional[str] = None
    id: Optional[int] = None
    must_change_password: bool = False
    session_version: int = 1
    created_at: object = field(default_factory=_utc_now)

    def __repr__(self):
        return f"<User id={self.id} username='{self.username}' type='{self.user_type}' farm='{self.farm_name}'>"

    @staticmethod
    def create_password_hash(password):
        """Хеширование пароля с криптографической солью."""
        return generate_password_hash(password)

    def check_password(self, password):
        """Проверка введенного пароля против сохраненного хеша."""
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    def is_admin(self):
        return self.user_type == 'admin'

    def is_farm_user(self):
        return self.user_type == 'farm'

    def to_dict(self):
        created_val = (
            self.created_at.strftime('%Y-%m-%d %H:%M:%S')
            if isinstance(self.created_at, datetime)
            else self.created_at
        )
        return {
            'id': self.id,
            'username': self.username,
            'user_type': self.user_type,
            'farm_name': self.farm_name,
            'must_change_password': bool(self.must_change_password),
            'created_at': created_val
        }


@dataclass
class Cow:
    """Модель карточки выбытия животного КРС."""
    cow_id: Optional[str] = None
    farm_name: Optional[str] = None
    category: Optional[str] = None
    reason: Optional[str] = None
    disposal_date: Optional[str] = None
    id: Optional[int] = None
    ear_tag: Optional[str] = None
    lactation: Optional[int] = None
    weight: Optional[float] = None
    notes: Optional[str] = None
    age_group: Optional[str] = None
    lactation_days: Optional[int] = None
    lactation_phase: Optional[str] = None
    pathology_system: Optional[str] = None
    breed: Optional[str] = None
    milk_yield: Optional[float] = None
    is_pregnant: int = 0
    gestation_days: int = 0
    is_stillborn: int = 0
    created_at: object = field(default_factory=_utc_now)
    created_by: Optional[int] = None
    updated_at: Optional[object] = None
    updated_by: Optional[int] = None

    def to_dict(self):
        d = asdict(self)
        for k in ('created_at', 'updated_at'):
            if isinstance(d[k], datetime):
                d[k] = d[k].strftime('%Y-%m-%d %H:%M:%S')
            elif d[k] is not None:
                d[k] = str(d[k])
        return d


@dataclass
class StillbirthRecord:
    """Модель учёта мертворождений и абортов (Акушерский журнал)."""
    record_code: Optional[str] = None
    farm_name: Optional[str] = None
    event_date: Optional[str] = None
    event_type: Optional[str] = None
    cause: Optional[str] = None
    id: Optional[int] = None
    mother_tag: Optional[str] = None
    mother_group: Optional[str] = None
    fetus_sex: Optional[str] = None
    fetus_weight: Optional[float] = None
    gestation_days: int = 0
    mother_condition: Optional[str] = None
    attendant: Optional[str] = None
    notes: Optional[str] = None
    created_at: object = field(default_factory=_utc_now)
    created_by: Optional[int] = None
    updated_at: Optional[object] = None
    updated_by: Optional[int] = None

    def to_dict(self):
        d = asdict(self)
        for k in ('created_at', 'updated_at'):
            if isinstance(d[k], datetime):
                d[k] = d[k].strftime('%Y-%m-%d %H:%M:%S')
            elif d[k] is not None:
                d[k] = str(d[k])
        return d
