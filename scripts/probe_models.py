"""Probe candidate models with real chat completions to find working ones."""

import os

import httpx
from dotenv import load_dotenv

load_dotenv()

SYSTEM = "Reply with exactly: MODEL_OK"


def probe_openai_compat(name: str, url: str, key: str, model: str, extra_headers=None) -> None:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "ping"},
        ],
        "max_tokens": 50,
        "temperature": 0,
    }
    try:
        r = httpx.post(url, headers=headers, json=payload, timeout=45)
        if r.status_code >= 400:
            print(f"  {name} / {model}: HTTP {r.status_code} -> {r.text[:160]}")
            return
        data = r.json()
        msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
        content = msg.get("content")
        if isinstance(content, list):
            content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
        text = (content or "").strip()
        reasoning = (msg.get("reasoning") or "")[:80]
        print(f"  {name} / {model}: OK content={text[:60]!r} reasoning={reasoning!r}")
    except Exception as e:
        print(f"  {name} / {model}: EXC {type(e).__name__} {str(e)[:160]}")


def probe_groq(model: str) -> None:
    from groq import Groq

    try:
        client = Groq(api_key=os.environ["GROQ_API_KEY"])
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": "ping"},
            ],
            max_tokens=50,
            temperature=0,
        )
        text = (resp.choices[0].message.content or "").strip()
        print(f"  groq / {model}: OK content={text[:60]!r}")
    except Exception as e:
        print(f"  groq / {model}: FAIL {str(e)[:160]}")


def main() -> None:
    print("== GROQ ==")
    for m in ("openai/gpt-oss-120b", "qwen/qwen3.8-27b", "groq/compound-mini"):
        probe_groq(m)

    print("== CEREBRAS ==")
    for m in ("gemma-4-31b", "gpt-oss-120b", "qwen-3.8-27b"):
        probe_openai_compat(
            "cerebras", "https://api.cerebras.ai/v1/chat/completions",
            os.environ["CEREBRAS_API_KEY"], m,
        )

    print("== GEMINI ==")
    for m in ("gemini-3.6-flash", "gemini-2.5-flash", "gemini-3.5-flash"):
        probe_openai_compat(
            "gemini", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            os.environ["GEMINI_API_KEY"], m,
        )

    print("== OPENROUTER (free models) ==")
    for m in ("google/gemma-4-31b-it:free", "nvidia/nemotron-3-ultra-550b-a55b:free"):
        probe_openai_compat(
            "openrouter", "https://openrouter.ai/api/v1/chat/completions",
            os.environ["OPENROUTER_API_KEY"], m,
            extra_headers={"HTTP-Referer": "https://mailbot.local", "X-Title": "Mailbot"},
        )


if __name__ == "__main__":
    main()
