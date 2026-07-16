"""P0 fix: process-wide safe_log_line enforcement.

Covers three things the 12-persona review flagged as one finding:
- ``_print_log`` itself must never raise when the record it was asked to log
  is secret-shaped -- it must fall back to a redacted stand-in instead of
  propagating ``safe_log_line``'s ValueError uncaught.
- ``install_safe_logging`` routes stdlib ``logging`` calls (e.g.
  ``engine.market_feed``'s 11 ``logger.warning()`` sites) through the same
  SECRET_PATTERNS contract, so a bare ``logging`` call can't bypass it.
- ``main()`` has a last-resort guard: any uncaught exception is logged as
  only its type name (never ``str(exc)``, which is exactly what a lower
  layer may have refused to log itself) and never reaches the default
  excepthook's raw traceback.
"""

from __future__ import annotations

import logging

import pytest

from engine import main as main_module
from engine.executor import install_safe_logging
from engine.main import _print_log, main


def test_print_log_falls_back_when_record_is_secret_shaped(capsys):
    _print_log({"event": "startup", "passphrase": "hunter2"})
    out = capsys.readouterr().out
    assert "hunter2" not in out
    assert "redacted" in out


def test_print_log_passes_ordinary_records_through_unchanged(capsys):
    _print_log({"event": "tick", "status": "ok"})
    out = capsys.readouterr().out
    assert '"status":"ok"' in out


@pytest.fixture()
def clean_root_logger():
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    yield root
    root.handlers[:] = original_handlers
    root.level = original_level


class TestInstallSafeLogging:
    def test_secret_shaped_warning_is_redacted(self, clean_root_logger, capsys):
        install_safe_logging()
        logger = logging.getLogger("engine.market_feed")
        logger.warning("leaked key %s", "0x" + "ab12cd34" * 8)
        err = capsys.readouterr().err
        assert "ab12cd34" not in err
        assert "redacted" in err

    def test_ordinary_warning_passes_through(self, clean_root_logger, capsys):
        install_safe_logging()
        logger = logging.getLogger("engine.market_feed")
        logger.warning("gamma request %s failed", "https://example.com/markets")
        err = capsys.readouterr().err
        assert "gamma request" in err
        assert "https://example.com/markets" in err


def test_main_fatal_exception_never_leaks_raw_message(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(main_module, "startup", lambda config_path, runtime_dir: object())

    def boom(config, runtime_dir, config_path=None):
        raise RuntimeError("boom passphrase=hunter2")

    monkeypatch.setattr(main_module, "build_loop", boom)

    code = main(["--config", "unused.yaml", "--runtime", str(tmp_path), "--once"])

    out = capsys.readouterr().out
    assert code == 1
    assert "hunter2" not in out
    assert "RuntimeError" in out
