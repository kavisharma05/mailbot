"""List available models on each configured LLM provider."""

import os

import httpx
from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    groq_key = os.environ.get("GROQ_API_KEY", "")
    try:
        r = httpx.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {groq_key}"},
            timeout=30,
        )
        ids = sorted(m["id"] for m in r.json().get("data", []))
        print("GROQ MODELS:")
        for i in ids:
            print("  ", i)
    except Exception as e:
        print("GROQ FAIL:", str(e)[:300])

    cerebras_key = os.environ.get("CEREBRAS_API_KEY", "")
    try:
        r = httpx.get(
            "https://api.cerebras.ai/v1/models",
            headers={"Authorization": f"Bearer {cerebras_key}"},
            timeout=30,
        )
        ids = sorted(m["id"] for m in r.json().get("data", []))
        print("CEREBRAS MODELS:")
        for i in ids:
            print("  ", i)
    except Exception as e:
        print("CEREBRAS FAIL:", str(e)[:300])

    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    try:
        r = httpx.get(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={gemini_key}",
            timeout=30,
        )
        names = sorted(m["name"] for m in r.json().get("models", []))
        print("GEMINI MODELS:")
        for n in names:
            print("  ", n)
    except Exception as e:
        print("GEMINI FAIL:", str(e)[:300])


if __name__ == "__main__":
    main()
