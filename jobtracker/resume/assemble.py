"""Turning a source document into a PDF. The one place this repo shells out.

Nothing else in jobtracker runs a subprocess — there is no `subprocess`, `Popen` or
`os.system` anywhere else in the package, and Playwright manages its own browser through
a vendored driver. So this module is the exception, and it is written to be the kind of
exception that does not become a habit: one function, one command built by a pure
`ResumeFormat.command()`, and every bound stated here rather than at the call site.

The bounds, and what each one is for:

* **No shell.** `subprocess.run` with a list, `shell=False` (the default, stated anyway).
  There is no shell to quote for, so there is nothing to quote wrong.
* **A scratch directory per run.** The source is copied into a fresh temp dir and the
  engine is run with that as its cwd, so a document that writes files writes them
  somewhere that is deleted a moment later — and cannot reach `data/` or the repo.
* **A timeout.** TeX loops. `\\def\\x{\\x}\\x` is a hang, not an error, and a nightly job
  that hangs is worse than one that fails.
* **No network, no shell-escape.** `latex.command()` passes `--untrusted`, which is
  tectonic's own refusal of `\\write18` and friends.
* **Output is read back as bytes and returned**, never left on disk for a caller to find.
  Where it is stored is the caller's decision, not this module's.

The guard that matters most is not here, though: `latex.sanitize()` refuses to put an
unknown control sequence into the document in the first place. This is the second line.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("jobtracker.resume")

# Long enough for a real resume on a cold tectonic cache (the first run fetches packages),
# short enough that a looping macro is a failure rather than a stuck nightly job.
TIMEOUT_S = 120


class AssemblyFailed(Exception):
    """The document did not compile, phrased for the person who has to fix it."""


def assemble(fmt, text: str, stem: str = "resume") -> bytes:
    """Compile `text` with `fmt` and return the PDF bytes.

    Raises `AssemblyFailed` with the engine's own last words on any failure — a missing
    toolchain, a non-zero exit, a timeout, or a run that reported success and produced no
    file. That last case is the one worth naming: a compile that "worked" and left nothing
    behind would otherwise be an empty PDF attached to an application.
    """
    reason = fmt.unavailable_reason()
    if reason:
        raise AssemblyFailed(reason)

    with tempfile.TemporaryDirectory(prefix="jobtracker-tex-") as scratch:
        work = Path(scratch)
        source = work / f"{stem}{fmt.suffix}"
        source.write_text(text, encoding="utf-8")

        argv = fmt.command(source.name)
        try:
            done = subprocess.run(  # noqa: S603 — argv list, no shell; see the docstring
                argv,
                cwd=work,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_S,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise AssemblyFailed(f"{argv[0]} is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise AssemblyFailed(
                f"{argv[0]} did not finish within {TIMEOUT_S}s and was stopped"
            ) from exc

        if done.returncode != 0:
            raise AssemblyFailed(_last_words(done.stderr or done.stdout))

        pdf = work / f"{stem}.pdf"
        if not pdf.is_file():
            # Exit 0 with no output. Reported rather than returned as empty bytes, which
            # would reach a real application as a blank attachment.
            raise AssemblyFailed(f"{argv[0]} reported success but produced no PDF")
        blob = pdf.read_bytes()

    log.info("assembled %s (%d bytes)", stem, len(blob))
    return blob


def _last_words(output: str) -> str:
    """The tail of the engine's output, which is where TeX puts the actual error."""
    lines = [ln.rstrip() for ln in (output or "").splitlines() if ln.strip()]
    if not lines:
        return "the document did not compile, and the engine said nothing"
    return "the document did not compile: " + " / ".join(lines[-3:])


def write_pdf(path: Path, blob: bytes) -> None:
    """Write the PDF where the caller asked, atomically.

    Same `.part`-then-replace as `resumes.write_atomic`, and for the same reason: this
    path may be read by a browser that is filling a form while it is being rewritten.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(blob)
    shutil.move(str(tmp), str(path))
