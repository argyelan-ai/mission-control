import json
from pathlib import Path

from app.models.runtime import Runtime
from app.services.harness_compat import is_compatible

REPO = Path(__file__).resolve().parents[2]
SEED = REPO / "backend" / "config" / "runtimes.json"


def test_ollama_cloud_seed_present_and_hermes_compatible():
    data = json.loads(SEED.read_text())
    entries = data if isinstance(data, list) else data.get("runtimes", data)
    oc = next(e for e in entries if e.get("id") == "ollama-cloud" or e.get("slug") == "ollama-cloud")
    assert oc["endpoint"] == "https://ollama.com/v1"
    assert oc["runtime_type"] == "openai_compatible"
    rt = Runtime(slug="ollama-cloud", display_name="Ollama Cloud",
                 runtime_type=oc["runtime_type"], endpoint=oc["endpoint"],
                 model_identifier=oc.get("model_identifier", "kimi-k2.6"), enabled=True)
    assert is_compatible("hermes", rt) is True
