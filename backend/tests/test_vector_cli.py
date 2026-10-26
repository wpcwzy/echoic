"""R-20 vector reindex CLI tests (dry-run / apply / error paths, no network)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Chunk
from app.services.vector_store import reset_vector_store_cache
from app.tools.vector_reindex import main
from tests.test_vector_helpers import (
    configure_embeddings,
    vdb,
    seed_recording,
)


@pytest.fixture(autouse=True)
def _clean_vector_state():
    reset_vector_store_cache()
    yield
    reset_vector_store_cache()


def _fresh_chunks(recording_id: int) -> list[Chunk]:
    with SessionLocal() as db:
        return list(
            db.scalars(
                select(Chunk)
                .where(Chunk.recording_id == recording_id)
                .order_by(Chunk.idx.asc())
            ).all()
        )


def test_cli_dry_run_does_not_write(vdb, capsys):
    configure_embeddings(vdb, "mock")
    recording = seed_recording(vdb, title="CLI dry-run", embed=False)

    exit_code = main(["--all", "--dry-run"])
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "dry-run" in output
    assert "处理录音 1 条" in output
    assert "待写入切片 3 个" in output
    assert all(chunk.embedding is None for chunk in _fresh_chunks(recording.id))


def test_cli_applies_for_recording_id(vdb, capsys):
    configure_embeddings(vdb, "mock")
    recording = seed_recording(vdb, title="CLI apply", embed=False)

    exit_code = main(["--recording-id", str(recording.id)])
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "写入切片 3 个" in output
    stored = _fresh_chunks(recording.id)
    assert all(chunk.embedding and chunk.dim == 256 for chunk in stored)
    assert all(chunk.embedding_model == "mock-embedding" for chunk in stored)

    skipped = main(["--all"])
    skipped_output = capsys.readouterr().out
    assert skipped == 0
    assert "写入切片 0 个" in skipped_output
    assert "跳过 3 个" in skipped_output

    forced = main(["--all", "--force"])
    forced_output = capsys.readouterr().out
    assert forced == 0
    assert "写入切片 3 个" in forced_output


def test_cli_requires_embeddings_provider(vdb, capsys):
    configure_embeddings(vdb, "none")
    exit_code = main(["--all", "--dry-run"])
    error = capsys.readouterr().err
    assert exit_code == 2
    assert "未配置 Embedding 模型" in error


def test_cli_unknown_recording_reports_error(vdb, capsys):
    configure_embeddings(vdb, "mock")
    exit_code = main(["--recording-id", "99999", "--dry-run"])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "99999" in captured.out
    assert "错误 1 条" in captured.out


def test_cli_requires_target_argument():
    with pytest.raises(SystemExit):
        main([])
