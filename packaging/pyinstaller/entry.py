"""Entry point for the frozen desktop app (PyInstaller).

With no verb it is `aquila gui`. The GUI starts the observer as a child process by
running this same executable with a verb (`run`, `hline`, `cal`), since a frozen
build has no separate Python interpreter; those verbs go straight to the CLI."""
import multiprocessing
import sys

from aquila.cli import main

VERBS = {"run", "hline", "cal", "curate", "status", "setup", "gui"}

if __name__ == "__main__":
    multiprocessing.freeze_support()
    argv = sys.argv[1:]
    main(argv if any(a in VERBS for a in argv) else argv + ["gui"])
