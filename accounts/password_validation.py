from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _


class LetterAndNumberPasswordValidator:
    """Keep the server policy aligned with the live registration checklist."""

    def validate(self, password, user=None):
        if not any(character.isalpha() for character in password) or not any(
            character.isdigit() for character in password
        ):
            raise ValidationError(
                _("Choose a password containing at least one letter and one number."),
                code="password_requires_letter_and_number",
            )

    def get_help_text(self):
        return _("Your password must contain at least one letter and one number.")
