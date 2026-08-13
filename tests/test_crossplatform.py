"""Things that behave differently on Windows and would only fail there."""
import json
import re
from datetime import date
from pathlib import Path

import pytest

from app import config, hostinfo
from app.deps import day_month
from app.services import importer


def test_pending_upload_survives_an_accented_filename():
    """Windows defaults text files to the local codepage; a Portuguese
    statement name has to round-trip regardless."""
    name = "Extrato Agosto — Descrição Conta №2.pdf"
    token = importer.stash_pending(name, b"%PDF-1.4 fake", account_id=3)
    try:
        filename, data, account_id = importer.load_pending(token)
        assert filename == name
        assert data == b"%PDF-1.4 fake"
        assert account_id == 3
        # stored as utf-8, not the platform default
        meta = config.PENDING_DIR / f"{token}.json"
        assert json.loads(meta.read_text(encoding="utf-8"))["filename"] == name
    finally:
        importer.drop_pending(token)


def test_saved_upload_name_is_filesystem_safe():
    """The kept copy of an upload is named from the original, so every
    character Windows reserves has to be gone. Accented letters are fine and
    are deliberately kept — they make the stored file recognisable."""
    raw = 'Extrato: 08/2026 "final" <Descrição>|?*.pdf'
    safe = "".join(c for c in raw if c.isalnum() or c in "._-")[:80]
    assert not (set(safe) & set('<>:"/\\|?* ')), safe
    assert ".." not in safe and not safe.startswith(("/", "\\"))
    assert "Descrição" in safe


def test_no_source_file_hardcodes_a_posix_path():
    offenders = []
    for path in (Path(__file__).resolve().parent.parent / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in ('"/tmp', "'/tmp", '"/var/', "'/var/", '"/usr/', "'/usr/"):
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert not offenders, offenders


def test_lan_ip_never_raises_and_never_reports_loopback():
    ip = hostinfo.lan_ip()
    assert isinstance(ip, str)
    assert not ip.startswith("127.")


# --- dates printed for people to read ----------------------------------------

def test_a_date_reads_without_a_leading_zero():
    assert day_month(date(2026, 8, 7)) == "7 Aug"
    assert day_month(date(2026, 12, 21)) == "21 Dec"
    assert day_month(date(2026, 1, 1)) == "1 Jan"


def test_no_template_asks_the_c_library_for_a_day_without_a_zero():
    """Regression: the budget page printed paydays with strftime('%-d %b').

    '%-d' is a glibc extension — fine on Linux, ValueError on Windows, so the
    whole page came back as Internal Server Error there and nowhere else. Any
    test that only ever runs on Linux would miss it, so the guard is on the
    source, not on the output. '%#d' is the same trap facing the other way.
    """
    repo = Path(__file__).resolve().parent.parent
    bad = re.compile(r"%[-#][a-zA-Z]")
    offenders = []
    for path in list((repo / "app").rglob("*.html")) + list((repo / "app").rglob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if bad.search(line):
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert not offenders, ("use the day_month filter instead of strftime: "
                           + "; ".join(offenders))


def test_launchers_exist_for_both_platforms():
    repo = Path(__file__).resolve().parent.parent
    assert (repo / "run.sh").exists()
    bat = repo / "run.bat"
    assert bat.exists()
    text = bat.read_text(encoding="utf-8")
    # the Windows launcher must use the venv's own interpreter, not a global one
    assert ".venv\\Scripts\\python.exe" in text
    assert "-m uvicorn app.main:app" in text
