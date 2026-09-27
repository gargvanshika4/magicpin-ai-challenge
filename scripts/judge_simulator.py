#!/usr/bin/env python3
"""Run the official judge_simulator.py scenarios against a running bot.

If LLM_API_KEY + LLM_PROVIDER are set, the simulator's real LLM scorer is used.
Otherwise a stub provider is injected: the rule-checked scenarios (warmup,
auto_reply_hell, intent_transition, hostile) run for real, and message
scores fall back to the simulator's own heuristic.

    BOT_URL=http://localhost:8080 python3 scripts/run_local_judge.py [scenario]
"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv


# Windows defaults to cp1252; the official simulator opens dataset files without an
# encoding, which would corrupt '₹'. Re-launch in UTF-8 mode if needed.
if sys.flags.utf8_mode == 0 and os.environ.get("PYTHONUTF8") != "1":
    os.environ["PYTHONUTF8"] = "1"
    import subprocess
    sys.exit(subprocess.call([sys.executable, "-X", "utf8", *sys.argv]))

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
if not ENV_FILE.exists():
    print(f"[warn] no .env found at {ENV_FILE}; using process environment only")
load_dotenv(ENV_FILE, override=False)
sys.path.insert(0, str(ROOT))
import judge_simulator as js  # noqa: E402


def env(*names, default=None):
    """First non-empty value among several accepted variable names."""
    for n in names:
        v = os.getenv(n)
        if v and v.strip():
            return v.strip()
    return default


js.BOT_URL = env("BOT_URL", default=f"http://localhost:{env('PORT', default='8080')}")
scenario = sys.argv[1] if len(sys.argv) > 1 else env("JUDGE_TEST_SCENARIO", "TEST_SCENARIO", default="all")


class StubLLM(js.LLMProvider):
    def name(self):
        return "stub (no API key — heuristic scoring)"

    def complete(self, prompt, system=None):
        raise RuntimeError("no LLM configured")


api_key = env("LLM_API_KEY", "JUDGE_LLM_API_KEY", "GEMINI_API_KEY")
if api_key:
    js.LLM_PROVIDER = env("LLM_PROVIDER", "JUDGE_LLM_PROVIDER", default="gemini").lower()
    js.LLM_API_KEY = api_key
    js.LLM_MODEL = env("LLM_MODEL", "JUDGE_LLM_MODEL", default="gemini-2.5-flash")
    llm = js.create_provider()
else:
    print("[info] no LLM key in .env/environment -> heuristic scoring")
    llm = StubLLM()

print(f"[config] bot={js.BOT_URL} scenario={scenario} "
      f"provider={js.LLM_PROVIDER if api_key else 'stub'} model={js.LLM_MODEL if api_key else '-'} "
      f"key={'set' if api_key else 'missing'}")

ok = js.JudgeSimulator(llm).run(scenario)
sys.exit(0 if ok else 1)
