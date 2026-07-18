"""Interactive approval batching for potentially destructive actions."""

from __future__ import annotations

from core.audit import InteractionLogger


class ApprovalManager:
    """Ask once, allow the rest of a batch, deny, or stop the task."""

    def __init__(self, logger: InteractionLogger | None = None) -> None:
        self.logger = logger
        self.remaining = 0
        self.allow_remaining = False
        self.cancelled = False

    def begin_batch(self, count: int) -> None:
        self.remaining = max(count, 0)
        self.allow_remaining = False
        self.cancelled = False

    def confirm(self, prompt: str) -> bool:
        if self.cancelled:
            self._log(prompt, "task already stopped")
            return False
        if self.allow_remaining:
            self.remaining = max(0, self.remaining - 1)
            self._log(prompt, "allowed by batch")
            return True

        import questionary

        choices = [
            questionary.Choice("Allow this action", value="once"),
        ]
        if self.remaining > 1:
            choices.append(
                questionary.Choice(
                    f"Allow all remaining actions in this step ({self.remaining})",
                    value="all",
                )
            )
        choices.extend(
            [
                questionary.Choice("Deny this action", value="deny"),
                questionary.Choice("Stop this task", value="stop"),
            ]
        )
        decision = questionary.select(
            f"{prompt}\nChoose with ↑/↓ and press Enter:",
            choices=choices,
            default="deny",
        ).ask()
        self.remaining = max(0, self.remaining - 1)
        if decision == "all":
            self.allow_remaining = True
            self._log(prompt, "allowed remaining batch")
            return True
        if decision == "once":
            self._log(prompt, "allowed once")
            return True
        if decision == "stop" or decision is None:
            self.cancelled = True
            self._log(prompt, "task stopped")
            return False
        self._log(prompt, "denied")
        return False

    def _log(self, prompt: str, decision: str) -> None:
        if self.logger:
            self.logger.approval(prompt, decision)
