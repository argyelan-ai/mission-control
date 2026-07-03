"""
Watchdog Service — periodic monitoring of all critical components.

Split into:
- core.py: WatchdogService orchestrator (start, stop, check_all)
- session_monitor.py: session-related checks (recovery, health, tokens)
- task_monitor.py: task-related checks (phases, queues, dispatches)
- health_checks.py: system and agent health checks
"""

from app.services.watchdog.core import WatchdogService

watchdog = WatchdogService()

__all__ = ["WatchdogService", "watchdog"]
