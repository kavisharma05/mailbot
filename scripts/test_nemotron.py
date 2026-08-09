"""Quick live check for Nemotron provider."""

from config import get_settings
from ai.client import _provider_chain, call_text, call_json, get_llm_status

get_settings.cache_clear()
settings = get_settings()
chain = [name for name, _ in _provider_chain()]
print("NEMOTRON_API_KEY set:", bool(settings.nemotron_api_key))
print("Provider order:", chain)
print("Primary model:", settings.nemotron_primary_model)
print("Brain model:", settings.nemotron_brain_model)
print()

print("--- Testing brain (call_text) ---")
text = call_text("Reply with exactly: NEMOTRON_OK", "ping", max_tokens=30, temperature=0)
print("Result:", repr(text))
status = get_llm_status()
print("Last OK provider:", status.get("last_ok_provider"))
print("Nemotron config:", status.get("nemotron"))
print()

print("--- Testing primary (call_json) ---")
data = call_json('Return JSON only: {"status": "ok"}', "test")
print("Result:", data)
status2 = get_llm_status()
print("Last OK provider:", status2.get("last_ok_provider"))
