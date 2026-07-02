"""
Watchdog Service — Periodische Ueberwachung aller kritischen Komponenten.

Aufgeteilt in:
- core.py: WatchdogService Orchestrator (start, stop, check_all)
- session_monitor.py: Session-bezogene Checks (Recovery, Health, Tokens)
- task_monitor.py: Task-bezogene Checks (Phasen, Queues, Dispatches)
- health_checks.py: System- und Agent-Health Checks
"""

from app.services.watchdog.core import WatchdogService

watchdog = WatchdogService()

__all__ = ["WatchdogService", "watchdog"]
