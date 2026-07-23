"""
Моніторинг: лічильники процесу, згруповані в клас `Metrics`.

In-memory лічильники лишаємо (їх читають healthcheck/metrics і тести), і
ПАРАЛЕЛЬНО дзеркалимо у Prometheus — щоб за кількох реплік метрики не
фрагментувались, а збирались скрейпером із /metrics.
"""

import time

from prometheus_client import Counter as PromCounter


class Metrics:
    """Лічильники write-операцій: in-memory dict + Prometheus-лічильники.

    `data` — той самий dict, що й раніше (сумісність із healthcheck/metrics і
    тестами, які читають `METRICS["writes_committed"]`). Інкремент іде ЄДИНИМИ
    точками — `record_commit()` / `record_denied()`.
    """

    def __init__(self) -> None:
        self.data = {
            "started_at": time.time(),
            "writes_committed": 0,   # рахується в Database.run_write (кожен commit)
            "writes_denied": 0,      # рахується в _require_role (RBAC відмова)
        }
        self._committed = PromCounter(
            "aerodefences_writes_committed_total", "Успішно закомічені write-операції"
        )
        self._denied = PromCounter(
            "aerodefences_writes_denied_total", "Відмови RBAC на write-операціях"
        )

    def record_commit(self) -> None:
        self.data["writes_committed"] += 1
        self._committed.inc()

    def record_denied(self) -> None:
        self.data["writes_denied"] += 1
        self._denied.inc()


metrics = Metrics()

# Backward-compat alias: healthcheck/metrics і тести читають dict напряму.
METRICS = metrics.data
