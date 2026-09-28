"""
Password strength validation shared by registration and password changes.

Rules enforced (mirrored 1:1 by the frontend checklist):
- at least 8 characters
- at least one uppercase letter
- at least one lowercase letter
- at least one digit
- at least one special character
"""

import re

from django.core.exceptions import ValidationError

SPECIAL_CHARACTERS = r"()[]{}|\\`~!@#$%^&*_-+=;:'\",<>./?"

RULES = [
    ("length", "Be at least 8 characters long", lambda p: len(p) >= 8),
    ("uppercase", "Include an uppercase letter", lambda p: re.search(r"[A-Z]", p) is not None),
    ("lowercase", "Include a lowercase letter", lambda p: re.search(r"[a-z]", p) is not None),
    ("number", "Include a number", lambda p: re.search(r"\d", p) is not None),
    ("special", "Include a special character", lambda p: re.search(r"[^A-Za-z0-9\s]", p) is not None),
]


def password_rule_results(password: str) -> list[tuple[str, bool]]:
    """Returns [(rule_id, passed)] — also used by the strength-check API."""
    return [(rule_id, check(password)) for rule_id, _, check in RULES]


class PasswordStrengthValidator:
    """Django AUTH_PASSWORD_VALIDATORS entry enforcing all five rules."""

    def validate(self, password: str, user=None) -> None:
        failed = [msg for rule_id, msg, check in RULES if not check(password)]
        if failed:
            raise ValidationError(
                [f"Password must {msg.lower()}." for msg in failed],
                code="password_weak",
            )

    def get_help_text(self) -> str:
        return (
            "Your password must be at least 8 characters and include an uppercase "
            "letter, a lowercase letter, a number, and a special character."
        )
