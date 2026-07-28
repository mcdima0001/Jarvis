"""Подсистема скиллов: базовый класс, контекст, обнаружение и менеджер."""

from .base import HealthStatus, Skill, SkillMeta
from .context import SkillContext
from .discovery import SkillCandidate, discover
from .manager import SkillManager, SkillRecord
from .scope import SkillScope

__all__ = [
    "HealthStatus",
    "Skill",
    "SkillCandidate",
    "SkillContext",
    "SkillManager",
    "SkillMeta",
    "SkillRecord",
    "SkillScope",
    "discover",
]
