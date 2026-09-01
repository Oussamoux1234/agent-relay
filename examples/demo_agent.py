"""A harmless adapter target used by the README demo."""

import json
import sys


def main() -> None:
    checkpoint_prompt = sys.stdin.read()
    print(
        json.dumps(
            {
                "agent": "demo-agent",
                "checkpoint_received": "auditable checkpoint" in checkpoint_prompt,
                "prompt_characters": len(checkpoint_prompt),
                "result": "Ready to continue from the shared workspace.",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
