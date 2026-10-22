"""Figures and diagrams in the morning pulse.

The article writers run sandboxed: they can read and write only the runtime
workspace, and the system Python they get has no plotting library. So mybot
keeps a small plotting environment *inside* that workspace (matplotlib +
numpy, installed by the server, not the agent) and gives each writer its own
output folder there. A writer that makes a figure saves a PNG and puts

    ![caption](figure:name.png)

on a line of its own. After the writers finish, the pulse copies each
referenced, valid PNG into state/briefings/figures/<day><suffix>/ under a
stable name and rewrites the reference to match; references to anything
missing or invalid are dropped. Every surface reads the same markdown:

- the menu app draws the image inline (GET /briefing/figure),
- Discord gets the image as an attachment where the line was,
- surfaces that can't show images get the caption in italics instead.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from app.logging_setup import get_logger

log = get_logger("mybot.briefing", "briefing.log")

FIGURE_RE = re.compile(r"^[ \t]*!\[([^\]\n]*)\]\(figure:([A-Za-z0-9][\w.-]{0,80})\)[ \t]*$", re.M)
NAME_RE = re.compile(r"^[A-Za-z0-9][\w.-]{0,100}\.png$")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_BYTES = 4 * 1024 * 1024
MAX_PER_ARTICLE = 2
PACKAGES = ["matplotlib", "numpy"]

_env_lock = threading.Lock()


def env_dir(runtime_dir: str | Path) -> Path:
    return Path(runtime_dir).expanduser() / "figure-env"


def figure_python(runtime_dir: str | Path, *, install: bool = True) -> str:
    """The command a sandboxed agent runs to plot (`<env>/bin/figure-python`),
    or "" when the environment can't be made. Installing needs the network,
    which the server has and the agents don't; it happens once."""
    root = env_dir(runtime_dir)
    wrapper = root / "bin" / "figure-python"
    with _env_lock:
        if wrapper.exists() and (root / ".ready").exists():
            return str(wrapper)
        if not install:
            return ""
        try:
            if not (root / "bin" / "python").exists():
                base = _base_python()
                if not base:
                    log.warning("pulse figures disabled: no Python outside the home folder "
                                "(the agents' sandbox can't read home); set BRIEFING_FIGURE_PYTHON")
                    return ""
                subprocess.run([base, "-m", "venv", str(root)], check=True,
                               capture_output=True, timeout=300)
            subprocess.run([str(root / "bin" / "python"), "-m", "pip", "install", "-q", *PACKAGES],
                           check=True, capture_output=True, timeout=900)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("pulse figures disabled: could not set up %s: %s", root, exc)
            return ""
        # The agent's home is unreadable, so matplotlib's cache and config
        # live beside the environment, and it never tries to open a window.
        wrapper.write_text(
            "#!/bin/sh\n"
            f'export MPLCONFIGDIR="{root}/mplconfig" MPLBACKEND=Agg\n'
            f'exec "{root}/bin/python" "$@"\n'
        )
        os.chmod(wrapper, 0o755)
        (root / "mplconfig").mkdir(exist_ok=True)
        (root / ".ready").write_text("ok\n")
        log.info("pulse figure environment ready at %s", root)
        return str(wrapper)


def _base_python() -> str:
    """An interpreter the sandboxed agents can run: the environment links to
    it and its standard library, so it must live outside the (unreadable)
    home folder — not a uv- or pyenv-managed Python under ~."""
    home = Path.home().resolve()
    explicit = os.environ.get("BRIEFING_FIGURE_PYTHON", "").strip()
    candidates = [explicit] if explicit else [
        "/opt/homebrew/bin/python3", "/usr/local/bin/python3", sys.executable, "/usr/bin/python3",
    ]
    for candidate in candidates:
        if not candidate or not os.path.exists(candidate):
            continue
        real = Path(candidate).resolve()
        if explicit or home not in real.parents:
            return str(real)
    return ""


def work_dir(runtime_dir: str | Path, run: str, index: int) -> Path:
    """A writer's own output folder in the agent-writable workspace."""
    path = Path(runtime_dir).expanduser() / "pulse-figures" / run / f"w{index}"
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def instructions(python: str, out_dir: Path) -> str:
    """What a writer is told about figures (empty when they're unavailable)."""
    if not python:
        return ""
    return (
        "\n\nFigures: if a picture would genuinely help the reader — a diagram of how a method "
        "works, a sketch of how two ideas connect, or a chart of real numbers — make ONE (two at "
        "most). Most articles need none; never add one for decoration. Draw it with Python and "
        f"matplotlib: write a script, run it with `{python} script.py`, and save a PNG into "
        f"`{out_dir}` (figsize about 8x4.5, dpi 200, white background, large readable labels, "
        "no title inside the image, colorblind-safe colors). Diagrams: boxes and arrows with "
        "short labels, laid out so nothing overlaps — check by looking at the image if you can. "
        "Charts: plot only numbers from a source you cite or from the owner's own work, never "
        "invented ones; a conceptual sketch must say so in its caption. Put the figure where it "
        "belongs in the text, on its own line, exactly as `![caption](figure:file-name.png)` — "
        "the caption is one plain sentence saying what it shows."
    )


def _valid_png(path: Path) -> bool:
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_BYTES:
            return False
        with path.open("rb") as handle:
            return handle.read(8) == PNG_MAGIC
    except OSError:
        return False


def collect(text: str, src: Path, dest: Path, prefix: str) -> str:
    """Copy the writer's referenced PNGs into the pulse's figure folder as
    `<prefix>-<name>` and rewrite the references; drop the rest."""
    kept = 0

    def one(match: re.Match[str]) -> str:
        nonlocal kept
        caption, name = match.group(1).strip(), match.group(2)
        source = src / name
        if kept >= MAX_PER_ARTICLE or not name.endswith(".png") or not _valid_png(source) \
                or source.resolve().parent != src.resolve():
            return ""
        dest.mkdir(parents=True, exist_ok=True)
        final = f"{prefix}-{name}"
        shutil.copyfile(source, dest / final)
        os.chmod(dest / final, 0o640)
        kept += 1
        return f"![{caption}](figure:{final})"

    return _tidy(FIGURE_RE.sub(one, text or ""))


def prune(text: str, dest: Path) -> str:
    """Drop references the editor or verifier invented or mangled."""
    return _tidy(FIGURE_RE.sub(
        lambda m: m.group(0).strip() if _valid_png(dest / m.group(2)) else "", text or ""))


def figures_in(text: str) -> list[dict[str, str]]:
    return [{"caption": m.group(1).strip(), "name": m.group(2)} for m in FIGURE_RE.finditer(text or "")]


def as_captions(text: str, style: str = "*") -> str:
    """For surfaces without images: each figure becomes its caption."""
    def one(match: re.Match[str]) -> str:
        caption = match.group(1).strip()
        return f"{style}Figure: {caption}{style}" if caption else ""
    return _tidy(FIGURE_RE.sub(one, text or ""))


def without(text: str) -> str:
    return _tidy(FIGURE_RE.sub("", text or ""))


def _tidy(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text)


def resolve(dest_root: Path, run: str, name: str) -> Path | None:
    """A served figure: only a plain PNG name inside that run's folder."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(\.[a-z]+)?", run) or not NAME_RE.match(name):
        return None
    path = dest_root / run / name
    return path if _valid_png(path) else None
