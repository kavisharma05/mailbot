"""Live diagnostic: test DB tables, Gmail auth, and each LLM provider individually."""

from __future__ import annotations

import json
import traceback

from config import get_settings

get_settings.cache_clear()
settings = get_settings()

print("=" * 60)
print("1) DATABASE (Supabase)")
print("=" * 60)
try:
    from services.db import check_db_health, get_db

    print("check_db_health:", check_db_health())
    db = get_db()
    for table in ("assignments", "contacts", "class_sessions", "processed_emails", "user_prefs"):
        try:
            res = db.table(table).select("*").limit(1).execute()
            rows = res.data or []
            print(f"  table {table}: OK (sample rows: {len(rows)})")
            if rows and table in ("contacts", "class_sessions"):
                print(f"    columns: {sorted(rows[0].keys())}")
        except Exception as exc:
            print(f"  table {table}: FAIL -> {exc}")
except Exception:
    traceback.print_exc()

print()
print("=" * 60)
print("2) GMAIL AUTH")
print("=" * 60)
try:
    from gmail_api.client import get_gmail_service

    service = get_gmail_service()
    profile = service.users().getProfile(userId="me").execute()
    print("  Gmail OK:", profile.get("emailAddress"))
except Exception as exc:
    print("  Gmail FAIL:", type(exc).__name__, str(exc)[:300])

print()
print("=" * 60)
print("3) LLM PROVIDERS (each tested individually)")
print("=" * 60)

import ai.client as client

system = "Reply with exactly: PROVIDER_OK"
user = "ping"

# Test each provider function directly, ignoring cooldowns
client._cooldowns.clear()

providers = []
if settings.nemotron_api_key:
    providers.append(("nemotron", client._call_nemotron))
if settings.groq_api_key:
    providers.append((
        "groq",
        lambda s, u, json_mode, max_tokens, temperature: client._call_groq(
            s, u, json_mode=json_mode, max_tokens=max_tokens, temperature=temperature,
            api_key=settings.groq_api_key, model=settings.groq_model, provider="groq",
        ),
    ))
if settings.cerebras_api_key:
    providers.append(("cerebras", client._call_cerebras))
if settings.gemini_api_key:
    providers.append(("gemini", client._call_gemini))
if settings.github_models_token:
    providers.append(("github_models", client._call_github_models))
if settings.openrouter_api_key:
    providers.append(("openrouter", client._call_openrouter))

for name, fn in providers:
    client._cooldowns.clear()
    try:
        # 200 tokens: reasoning models need headroom; 30 would false-alarm.
        text = fn(system, user, json_mode=False, max_tokens=200, temperature=0.0)
        err, status, prov = client.get_last_llm_error()
        if text:
            print(f"  {name}: OK -> {text[:60]!r}")
        else:
            print(f"  {name}: FAIL (status={status}) -> {(err or '')[:200]}")
    except Exception as exc:
        print(f"  {name}: EXCEPTION -> {type(exc).__name__}: {str(exc)[:200]}")

print()
print("=" * 60)
print("4) END-TO-END: call_text / call_json through the chain")
print("=" * 60)
client._cooldowns.clear()
out = client.call_text("Reply with exactly: CHAIN_OK", "ping", max_tokens=30, temperature=0)
print("  call_text:", repr(out))
client._cooldowns.clear()
out = client.call_json('Return JSON only: {"status":"ok"}', "test")
print("  call_json:", out)

print()
print("=" * 60)
print("5) CONTACT RESOLUTION (DB-backed)")
print("=" * 60)
try:
    from services.contacts import resolve_contact

    matches = resolve_contact("test", min_score=0.1)
    print("  resolve_contact ran, matches:", len(matches))
except Exception as exc:
    print("  resolve_contact FAIL:", type(exc).__name__, str(exc)[:300])
