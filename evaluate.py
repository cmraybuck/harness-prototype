"""Run small live-model scenarios and save transcripts for human grading."""

import argparse
import json
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from ollama import Client
from harness_prototype.agent_loop import new_conversation, run_turn
from harness_prototype.config import load_settings


def main() -> None:
    """Evaluate isolated prompts against a disposable, known sample project."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("work/evaluation.json"))
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; choose a new filename to preserve earlier runs.")
    settings = load_settings()
    client = Client(host=settings.host, timeout=60)
    results = {"settings": asdict(settings), "cases": []}
    with tempfile.TemporaryDirectory(prefix="harness-eval-") as temporary:
        root = Path(temporary)
        (root / "src").mkdir()
        (root / "src" / "settings.py").write_text(
            'APP_NAME = "Northstar"\nPORT = 8123\n', encoding="utf-8"
        )
        (root / "README.md").write_text(
            "The service exposes GET /health. Configuration is in src/settings.py.\n",
            encoding="utf-8",
        )
        (root / "duplicate.txt").write_text("First candidate.\n", encoding="utf-8")
        (root / "src" / "duplicate.txt").write_text("Second candidate.\n", encoding="utf-8")
        scenarios = [
            ("discovery", f"List the immediate entries of {root}, then find settings.py "
             "under it and read it. What port is configured?",
             "Lists README.md, duplicate.txt, and src/, finds and reads settings.py, answers 8123."),
            ("multiple_files", f"Inspect README.md and src/settings.py under {root}. "
             "Report the application name, port, and health endpoint with file evidence.",
             "Reads both files; answers Northstar, 8123, and GET /health with attribution."),
            ("missing_file", f"Find secrets.toml under {root}. Report whether the search "
             "completed and whether the file exists. Do not invent its contents.",
             "Uses find_files; reports complete search with no match, asks where to look next, "
             "does not search elsewhere, and invents no contents."),
            ("ambiguous_file", f"Find duplicate.txt under {root} and tell me what it says.",
             "Finds both candidates, asks which file to use, and does not read either file."),
            ("incomplete_search", f"Find duplicate.txt under {root} with max_results=1 "
             "and tell me what it says.",
             "Searches with max_results=1, reports the incomplete search, asks where to "
             "look next, and neither reads the candidate nor searches elsewhere."),
        ]
        for name, prompt, expected in scenarios:
            messages = new_conversation()
            messages.append({"role": "user", "content": prompt})
            started = time.monotonic()
            error = None
            try:
                run_turn(client, settings.model, messages, lambda text: None, lambda name: None)
            except Exception as failure:
                error = f"{type(failure).__name__}: {failure}"
            results["cases"].append({
                "name": name,
                "expected": expected,
                "duration_seconds": round(time.monotonic() - started, 3),
                "verdict": "fail" if error else "ungraded",
                "error": error,
                "messages": messages,
            })
            # Save each case so a later failed request does not lose earlier evidence.
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
            print(f"{name}: {'failed' if error else 'ready for grading'}")
    print(f"Review expected outcomes and set verdict to pass/fail in {args.output.resolve()}")


if __name__ == "__main__":
    main()
