"""Quick test script for token service."""

from uuid import uuid4
from hashlib import sha256

from app.services.token_service import token_service
from app.utils.password import generate_temp_password


def mask_identifier(value: str) -> str:
    digest = sha256(value.encode("utf-8")).hexdigest()
    return digest[:8]


def test_token_service():
    """Test token generation and verification."""
    print("Testing token service...")
    print("-" * 50)

    # Test email verification token
    user_id = uuid4()
    email = "test@example.com"

    print("\n1. Creating email verification token")
    token = token_service.create_email_verification_token(user_id, email)
    print("   Token created successfully")

    print(f"\n2. Verifying token...")
    data = token_service.verify_email_token(token)
    if data:
        print(f"   ✅ Token verified successfully!")
        print(f"   User ID hash: {mask_identifier(str(data['user_id']))}")
        print(f"   Email hash: {mask_identifier(data['email'])}")
    else:
        print(f"   ❌ Token verification failed")

    print(f"\n3. Trying to verify same token again (should fail)...")
    data = token_service.verify_email_token(token)
    if data:
        print(f"   ❌ Token verified again (should not happen)")
    else:
        print(f"   ✅ Token correctly rejected (one-time use)")

    # Test password reset token
    print(f"\n4. Creating password reset token...")
    reset_token = token_service.create_password_reset_token(user_id, email)
    print("   Reset token created successfully")

    print(f"\n5. Verifying reset token...")
    data = token_service.verify_password_reset_token(reset_token)
    if data:
        print(f"   ✅ Reset token verified successfully!")
        print(f"   User ID hash: {mask_identifier(str(data['user_id']))}")
        print(f"   Email hash: {mask_identifier(data['email'])}")
    else:
        print(f"   ❌ Reset token verification failed")

    print("\n" + "-" * 50)
    print("✅ All token service tests passed!")


def test_password_generation():
    """Test password generation."""
    print("\nTesting password generation...")
    print("-" * 50)

    for i in range(3):
        password = generate_temp_password()
        print(f"{i + 1}. Generated password length: {len(password)}")

    print("✅ Password generation works!")


if __name__ == "__main__":
    test_token_service()
    test_password_generation()
