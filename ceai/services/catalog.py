from __future__ import annotations

from typing import Any, Dict, List

from ceai.database import Database
from ceai.repositories.model_prices import ModelPriceRepository
from ceai.repositories.plans import PlanRepository


class CatalogService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.plans = PlanRepository()
        self.models = ModelPriceRepository()

    def list_plans(self) -> List[Dict[str, Any]]:
        with self.db.transaction() as conn:
            return self.plans.list_active(conn)

    def list_models(self) -> List[Dict[str, Any]]:
        with self.db.transaction() as conn:
            return self.models.list_active(conn)
