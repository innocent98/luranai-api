"""Password utilities for generation and validation."""

import secrets
import string


def generate_temp_password(length: int = 12) -> str:
    """
    Generate a secure temporary password.

    The password will contain:
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one digit
    - At least one special character

    Args:
        length: Length of password (default 12)

    Returns:
        Generated password string
    """
    if length < 8:
        raise ValueError("Password length must be at least 8 characters")

    # Define character sets
    uppercase = string.ascii_uppercase
    lowercase = string.ascii_lowercase
    digits = string.digits
    special = "!@#$%^&*"

    # Ensure at least one of each type
    password = [
        secrets.choice(uppercase),
        secrets.choice(lowercase),
        secrets.choice(digits),
        secrets.choice(special),
    ]

    # Fill the rest randomly
    all_chars = uppercase + lowercase + digits + special
    password.extend(secrets.choice(all_chars) for _ in range(length - 4))

    # Shuffle to avoid predictable patterns
    password_list = list(password)
    secrets.SystemRandom().shuffle(password_list)

    return "".join(password_list)
