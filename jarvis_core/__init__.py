"""jarvis_core — die kanal-agnostische Jarvis-Persona, Tools und Text-Gehirn.

Geteiltes Package fuer alle Jarvis-Kanaele (ADR-061):
- ``persona``   — kanal-agnostischer System-Prompt + Kanal-Addenda.
- ``channels``  — Kanal-Definitionen (Voice, Telegram) mit Capabilities.
- ``tools``     — provider-neutrale Tool-Specs + Handler (mit Kanal-Degradation).
- ``mc_client`` — transport-agnostischer HTTP-Client gegen das MC-Backend.
- ``brain``     — ``JarvisBrain``: Text-Modus mit OpenAI Function-Calling.

Der ``voice_worker`` (LiveKit) importiert Persona + Tools + mc_client; das
Backend (Telegram-Inbound) importiert zusaetzlich ``brain``. ``brain`` wird
NICHT eager importiert, damit reine Voice-Umgebungen ohne dessen Nutzung
schlank bleiben.
"""

from jarvis_core import channels, persona, tools

__all__ = ["channels", "persona", "tools"]
