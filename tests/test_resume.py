"""The resume format registry, and the one place this repo shells out.

`assemble.py` is the only subprocess in jobtracker. Nothing else runs one — no
`subprocess`, no `Popen`, no `os.system` anywhere else in the package — so the tests here
are mostly about it staying the kind of exception that does not become a habit: a list
argv with no shell, a scratch directory, a timeout, and a failure that says why.

The other half is the registry itself, which follows the house pattern: one module plus
one import line, and the module is pure.
"""

import ast
import importlib
import inspect
import pathlib
import re

import pytest

from jobtracker import config
from jobtracker.resume import AssemblyFailed, for_path, formats, get_format

# `jobtracker.resume` re-exports the `assemble` FUNCTION, which shadows the module of the
# same name — so the plain `from ... import assemble` a caller wants is not the module
# these tests need to read.
assemble_mod = importlib.import_module("jobtracker.resume.assemble")


# -- the registry --------------------------------------------------------------------
def test_latex_is_registered_and_owns_tex():
    assert get_format("latex") is not None
    assert for_path("/somewhere/resume.tex").name == "latex"


def test_a_format_nothing_handles_is_none_rather_than_a_guess():
    """A resume in a format we cannot read is an absence to report, not a file to
    attempt. `_load_resume` logs it and `tailor` reports itself unavailable."""
    assert for_path("/somewhere/resume.pdf") is None
    assert for_path("no-suffix-at-all") is None


def test_every_format_declares_a_name_and_a_suffix():
    for fmt in formats():
        assert fmt.name and fmt.suffix.startswith(".")


def _code_without_prose(module) -> str:
    """The module's source with every docstring removed.

    The same helper `tests/test_browser.py` needs, for the same reason and against the
    same trap: `assemble.py`'s prose *names* `os.system` and `shell=True` in order to
    explain that it does not use them, so a scan of the raw text finds the very strings
    it is asserting are absent. Assert on the code.
    """
    src = inspect.getsource(module)
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                src = src.replace(doc, "")
    return src


# -- the subprocess, and its bounds --------------------------------------------------
def test_the_command_never_enables_shell_escape():
    """A resume is compiled from text a model composed.

    `--untrusted` is tectonic's own refusal of \\write18 and friends, and it is the second
    line behind `latex.sanitize`. Either alone would probably do; the pair is what makes
    "probably" not matter.
    """
    argv = get_format("latex").command("resume.tex")
    assert "--untrusted" in argv
    assert not any("shell-escape" in a for a in argv)


def test_assembly_is_run_without_a_shell():
    """`shell=False` and a list argv, so there is no quoting to get wrong.

    Read off the source because the alternative is running a TeX engine in a unit test.
    """
    source = _code_without_prose(assemble_mod)
    assert "shell=False" in source
    assert "shell=True" not in source
    assert "os.system" not in source


def test_assembly_is_bounded_by_a_timeout():
    """TeX loops. `\\def\\x{\\x}\\x` is a hang, not an error, and a nightly job that hangs
    is worse than one that fails."""
    source = _code_without_prose(assemble_mod)
    assert "timeout=TIMEOUT_S" in source
    assert assemble_mod.TIMEOUT_S > 0


def test_a_missing_engine_is_reported_and_never_raises_something_else(monkeypatch):
    """A box with no TeX installed is a normal state, and it must be legible as one.

    This is the capability-absent-but-green shape: `tailor build` says what is missing and
    exits 0, because every suggestion it holds is still good.
    """
    fmt = get_format("latex")
    monkeypatch.setattr("jobtracker.resume.latex.shutil.which", lambda _: None)
    assert fmt.unavailable_reason() is not None
    with pytest.raises(AssemblyFailed):
        assemble_mod.assemble(fmt, "\\documentclass{article}\\begin{document}x\\end{document}")


def test_this_is_the_only_module_in_the_package_that_shells_out():
    """Stated as a test because it is the property that keeps it reviewable.

    If a second place needs a subprocess, that is a real decision to make deliberately —
    not something that should arrive as a diff nobody noticed.
    """
    package = pathlib.Path(config.__file__).parent
    # Code, not prose: several modules discuss the fact that this repo does not shell out,
    # and a substring search over docstrings would report them for saying so.
    calls = re.compile(r"^\s*(import subprocess|from subprocess|.*subprocess\.\w|"
                       r".*os\.system\(|.*Popen\()", re.MULTILINE)
    offenders = []
    for source in package.rglob("*.py"):
        if source.name == "assemble.py":
            continue
        text = source.read_text()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    text = text.replace(doc, "")
        text = "\n".join(
            line for line in text.splitlines()
            if not line.lstrip().startswith("#")
        )
        if calls.search(text):
            offenders.append(source.relative_to(package).as_posix())
    assert offenders == [], offenders
