"""Per-job budgets. Hermes let one stuck listing block 183 others for 90 minutes and reach eight
attempts; here every run carries a step cap, a per-portal wall clock and a failed-submit cap,
all persisted so a restart cannot reset them."""
from __future__ import annotations

import time

from . import db
from .config import SETTINGS


class BudgetExceeded(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Parked(RuntimeError):
    """Raised inside a run to end it with a named state. `state` is one of hold | ready_for_you |
    awaiting_* (the worker maps it). `blocked_on` carries question keys for holds."""

    def __init__(self, reason: str, state: str = "hold", blocked_on: list[str] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.state = state
        self.blocked_on = blocked_on or []


class Budget:
    def __init__(self, job_id: int, portal: str, steps_used: int = 0, wall_ms_used: int = 0,
                 failed_submits: int = 0):
        self.job_id = job_id
        self.max_steps = SETTINGS.budgets.max_steps
        self.wall_s = SETTINGS.wall_budget_s(portal)
        self.max_failed_submits = SETTINGS.budgets.max_failed_submits
        self.steps = steps_used
        self.wall_ms_before = wall_ms_used
        self.failed_submits = failed_submits
        self.started = time.monotonic()

    @property
    def wall_ms(self) -> int:
        return self.wall_ms_before + int((time.monotonic() - self.started) * 1000)

    def remaining_s(self) -> float:
        return self.wall_s - self.wall_ms / 1000.0

    def step(self) -> None:
        self.steps += 1
        self._persist()
        if self.max_steps and self.steps > self.max_steps:
            raise BudgetExceeded(f"budget_spent: steps>{self.max_steps}")
        if self.remaining_s() <= 0:
            raise BudgetExceeded(f"budget_spent: wall>{self.wall_s}s")

    def failed_submit(self) -> None:
        self.failed_submits += 1
        self._persist()
        if self.failed_submits >= self.max_failed_submits:
            raise BudgetExceeded(f"budget_spent: failed_submits>={self.max_failed_submits}")

    def check(self) -> None:
        if self.remaining_s() <= 0:
            raise BudgetExceeded(f"budget_spent: wall>{self.wall_s}s")

    def _persist(self) -> None:
        with db.tx() as con:
            con.execute("UPDATE jobs SET steps_used=?, wall_ms_used=?, failed_submits=? WHERE id=?",
                        (self.steps, self.wall_ms, self.failed_submits, self.job_id))
