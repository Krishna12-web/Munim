"""Claude tool-use loop. The model can only read data through the functions below."""
import json, os
import anthropic
import core

ANTHROPIC_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")
EMPTY = {"type": "object", "properties": {}}
TOOLS = [
    {"name": "get_latest_day", "description": "Latest day in the data: total, payment count, largest payment, average of previous 6 days, percent versus that average.", "input_schema": EMPTY},
    {"name": "get_week_totals", "description": "Daily collections for the last 7 days.", "input_schema": EMPTY},
    {"name": "get_forecast", "description": "Forecast for the next 7 days with a rough range, plus backtest error against a naive baseline.", "input_schema": EMPTY},
    {"name": "get_unusual_days", "description": "Recent days whose revenue was unusually high or low after accounting for weekday and trend.", "input_schema": EMPTY},
    {"name": "find_lapsed_customers", "description": "Regular named customers who probably stopped visiting, with probability, typical gap and monthly value at risk.", "input_schema": EMPTY},
]


def dispatch(name, df):
    d = core.daily(df)
    return {"get_latest_day": lambda: core.latest_day(df), "get_week_totals": lambda: core.week_totals(df),
            "get_forecast": lambda: core.forecast(d) or {"error": "need at least 14 days of data"},
            "get_unusual_days": lambda: core.anomalies(d), "find_lapsed_customers": lambda: core.lapsed(df)}[name]()


def ask(question, lang, df):
    system = ("You are Munim, a business assistant for a small shop owner in India. Answer in %s, in at most "
              "three short sentences, plain words, amounts with the rupee sign. Use ONLY data returned by the tools. "
              "If the tools cannot answer, say so in one sentence. Never invent numbers."
              % ("Hindi (Devanagari script)" if lang == "hi" else "English"))
    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    if openrouter_key:
        client = anthropic.Anthropic(
            api_key=openrouter_key,
            base_url="https://openrouter.ai/api",
        )
        model = OPENROUTER_MODEL
    else:
        client = anthropic.Anthropic()
        model = ANTHROPIC_MODEL
    msgs, log = [{"role": "user", "content": question}], []
    for _ in range(6):
        r = client.messages.create(model=model, max_tokens=700, system=system, tools=TOOLS, messages=msgs)
        if r.stop_reason != "tool_use":
            return {"answer": "".join(b.text for b in r.content if b.type == "text").strip(), "sources": log}
        msgs.append({"role": "assistant", "content": r.content})
        results = []
        for b in r.content:
            if b.type == "tool_use":
                out = dispatch(b.name, df)
                log.append("%s: %s" % (b.name, json.dumps(out, ensure_ascii=False)[:300]))
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(out)})
        msgs.append({"role": "user", "content": results})
    return {"answer": "I could not finish that question.", "sources": log}
