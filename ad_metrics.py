"""
Моніторинг: лічильники процесу.

In-memory METRICS лишаємо (використовується локально/у тестах), і ПАРАЛЕЛЬНО
дзеркалимо у Prometheus-лічильники — щоб за кількох реплік метрики не
фрагментувались, а збирались скрейпером із /metrics (див. entry-point).
"""

import time

from prometheus_client import Counter as PromCounter

METRICS = {
    "started_at": time.time(),
    "writes_committed": 0,   # рахується в _run_write (кожен commit)
    "writes_denied": 0,      # рахується в _require_role (RBAC відмова)
}
PROM_WRITES_COMMITTED = PromCounter(
    "aerodefences_writes_committed_total", "Успішно закомічені write-операції"
)
PROM_WRITES_DENIED = PromCounter(
    "aerodefences_writes_denied_total", "Відмови RBAC на write-операціях"
)
