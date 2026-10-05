"""Minimal programmatic example: run one goal and print each step.

Requires a Chrome session with remote debugging enabled (see README "Setup")
and a `.env` with `OPENROUTER_API_KEY`. Run from the repo root with:

    uv run --env-file .env examples/run_goal.py
"""

from jev_ultrafast.agent import Agent


def main() -> None:
    agent = Agent(
        "https://www.wikipedia.org/",
        "Open the Wikipedia article about Marie Curie.",
    )
    try:
        for state in agent.run():
            action = state["history"][-1]["action"] if state["history"] else None
            print(f"{state['status']:<12} {state['page']['url']}  <- {action}")
    finally:
        agent.close()


if __name__ == "__main__":
    main()
