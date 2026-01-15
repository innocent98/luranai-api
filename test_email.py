"""Quick test script for email service."""

from app.services.email_service import email_service

def test_email():
    """Test sending a welcome email."""
    print("Testing email service with Gmail SMTP...")
    print(f"SMTP Host: smtp.gmail.com")
    print(f"SMTP Port: 465 (SSL)")
    print(f"From: tosinadebayo55@gmail.com")
    print("-" * 50)

    success = email_service.send_welcome_email(
        company_name="Test Company",
        admin_email="tosinadebayo55@gmail.com",  # Sending to same email for testing
        admin_name="Test Admin",
        temp_password="TestPass123!",
        verification_token=None,  # No verification for test
    )

    if success:
        print("✅ Email sent successfully!")
        print("Check your inbox: tosinadebayo55@gmail.com")
    else:
        print("❌ Email failed to send. Check logs for details.")

    return success

if __name__ == "__main__":
    test_email()
