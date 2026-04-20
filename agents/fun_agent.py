from pathlib import Path

SYSTEM_PROMPT_FILE = Path(__file__).parent.parent / "system_prompts/fun.txt"

KID_CONTENT_RULE = (
    "This sender is a child (12 or under). Keep all jokes and riddles "
    "completely clean and age-appropriate — no adult themes, no edgy humor, "
    "no references to drinking, violence, or anything a 12-year-old shouldn't hear."
)

ADULT_CONTENT_RULE = (
    "This sender is an adult. Jokes can be a bit more grown-up — dry humor, "
    "wordplay, mild sarcasm — but keep it appropriate for a family group chat. "
    "Nothing explicit or offensive."
)


def load_system_prompt(is_kid: bool) -> str:
    base = SYSTEM_PROMPT_FILE.read_text()
    rule = KID_CONTENT_RULE if is_kid else ADULT_CONTENT_RULE
    return f"{base}\n\nContent rule for this sender: {rule}"
