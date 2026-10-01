"""reZme fase 2: base de conocimiento estructurada y verificable.

Solo biblioteca estándar: los tests del CI no instalan dependencias.
"""
from .schema import SCHEMA_VERSION, Claim, Entity, Implication, ValidationError
from .store import AmbiguousEntity, Store

__all__ = ["SCHEMA_VERSION", "Claim", "Entity", "Implication", "ValidationError",
           "AmbiguousEntity", "Store"]
