"""Test for unknown admission recovery under burst contention."""
import asyncio
import sqlite3
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestUnknownAdmissionRecovery:
    """Test that unknown admission status is recovered via re-read + re-submit."""

    @pytest.fixture
    def mock_client(self):
        """Mock gateway client."""
        client = AsyncMock()
        client.events = asyncio.Queue()
        return client

    @pytest.fixture
    def mock_claim(self):
        """Mock claim with admission already set."""
        from dataclasses import dataclass
        @dataclass
        class Claim:
            answered: float
            admission_id: str = "test_admission_id"
            session_id: str = "test_session_id"
        return Claim(answered=time.monotonic())

    @pytest.fixture
    def temp_board_db(self, tmp_path):
        """Create a temporary kanban board database with a running task."""
        db_path = tmp_path / "kanban.db"
        conn = sqlite3.connect(str(db_path))
        
        # Create basic schema (simplified)
        conn.executescript("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                current_run_id INTEGER NOT NULL
            );
            CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                status TEXT NOT NULL,
                claim_lock TEXT,
                run_id INTEGER
            );
        """)
        
        # Insert a running task
        conn.execute(
            "INSERT INTO tasks (id, status, current_run_id) VALUES (?, ?, ?)",
            ("task_test_001", "running", 1)
        )
        
        conn.commit()
        conn.close()
        return str(db_path)

    @pytest.fixture
    def worker_params(self):
        """Worker parameters."""
        return {
            'board': 'test_board',
            'task_id': 'task_test_001',
            'run_id': 1,
            'claim_lock': 'test_claim_lock'
        }

    @pytest.mark.asyncio
    async def test_unknown_recovery_succeeds_on_retry(self, mock_client, mock_claim, temp_board_db, worker_params):
        """Test that unknown admission is recovered when re-submission succeeds."""
        from hermes_cli.kanban_worker_client import _recover_unknown_admission
        
        # First response is unknown, second is terminal (recovery)
        call_count = [0]
        def mock_rpc(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call (initial receipt): return unknown
                return {'status': 'unknown', 'admission_id': mock_claim.admission_id}
            else:
                # Second call (recovery): return terminal
                return {
                    'status': 'terminal',
                    'exit_code': 0,
                    'receipt': {'status': 'terminal'}
                }
        
        mock_client.rpc = mock_rpc
        
        initial_receipt = {'status': 'unknown', 'admission_id': mock_claim.admission_id}
        
        result = await _recover_unknown_admission(
            mock_client, worker_params, temp_board_db, mock_claim, initial_receipt
        )
        
        # Should have recovered successfully
        assert result['status'] == 'terminal', f"Expected terminal, got {result['status']}"
        assert call_count[0] == 2, f"Expected 2 RPC calls, got {call_count[0]}"

    @pytest.mark.asyncio
    async def test_unknown_recovery_fails_after_exhausting_retries(self, mock_client, mock_claim, temp_board_db, worker_params):
        """Test that unknown admission fails after exhausting retries."""
        from hermes_cli.kanban_worker_client import _recover_unknown_admission
        
        # All re-submission attempts fail
        call_count = [0]
        def mock_rpc(*args, **kwargs):
            call_count[0] += 1
            return {'status': 'unknown', 'admission_id': mock_claim.admission_id}
        
        mock_client.rpc = mock_rpc
        
        initial_receipt = {'status': 'unknown', 'admission_id': mock_claim.admission_id}
        
        result = await _recover_unknown_admission(
            mock_client, worker_params, temp_board_db, mock_claim, initial_receipt
        )
        
        # Should have exhausted retries and returned original unknown
        assert result['status'] == 'unknown', f"Expected unknown, got {result['status']}"
        # Should have tried 3 retries plus initial
        assert call_count[0] >= 3, f"Expected at least 3 RPC calls, got {call_count[0]}"

    @pytest.mark.asyncio
    async def test_unknown_recovery_detects_closed_run(self, mock_client, mock_claim, temp_board_db, worker_params):
        """Test that unknown recovery detects when run has closed."""
        from hermes_cli.kanban_worker_client import _recover_unknown_admission
        import sqlite3
        
        # Update DB to show task is no longer running
        conn = sqlite3.connect(temp_board_db)
        conn.execute("UPDATE tasks SET status = 'completed'")
        conn.commit()
        conn.close()
        
        def mock_rpc(*args, **kwargs):
            # This should not be called if run is detected as closed
            raise AssertionError("RPC should not be called for closed run")
        
        mock_client.rpc = mock_rpc
        
        initial_receipt = {'status': 'unknown', 'admission_id': mock_claim.admission_id}
        
        result = await _recover_unknown_admission(
            mock_client, worker_params, temp_board_db, mock_claim, initial_receipt
        )
        
        # Should have returned original unknown without re-submitting
        assert result['status'] == 'unknown', f"Expected unknown, got {result['status']}"

    @pytest.mark.asyncio
    async def test_await_receipt_handles_unknown(self, mock_client, mock_claim, temp_board_db, worker_params):
        """Test that _await_receipt invokes recovery on unknown status."""
        from hermes_cli.kanban_worker_client import _await_receipt
        
        # Initial receipt is unknown
        call_count = [0]
        async def mock_receipt(client, claim):
            call_count[0] += 1
            if call_count[0] == 1:
                return {'status': 'unknown'}
            else:
                return {'status': 'terminal'}  # Recovery succeeded
        
        mock_client.rpc = AsyncMock(side_effect=lambda *args, **kwargs: asyncio.coroutine(lambda: {
            'status': 'terminal',
            'receipt': {'status': 'terminal'},
            'session_id': 'recovered_session',
            'admission_id': 'recovered_admission'
        })())
        
        # Add _receipt function for mock
        import hermes_cli.kanban_worker_client as client_module
        original_receipt = client_module._receipt
        client_module._receipt = mock_receipt
        
        try:
            result = await _await_receipt(mock_client, worker_params, temp_board_db, mock_claim)
            assert result['status'] == 'terminal', f"Expected terminal after recovery, got {result['status']}"
        finally:
            client_module._receipt = original_receipt
