# Email Addresses Used in Luran System

## Summary

**Total Email Addresses Needed: 1** (for sending emails)

---

## Primary Sending Email ⭐

### `noreply@luranai.com`

**Purpose**: All automated system emails

**Environment Variables**:
- `SMTP_USER=noreply@luranai.com`
- `EMAILS_FROM_EMAIL=noreply@luranai.com`

**Used for sending**:
1. ✉️ Welcome emails (new user onboarding)
2. ✉️ User invitation emails (team member invites)
3. ✉️ Password reset emails (forgot password flow)
4. ✉️ Password changed notifications (security alerts)
5. ✉️ Email verification emails (account activation)
6. ✉️ Trial expiring warnings (7 days before trial ends)
7. ✉️ Subscription confirmed emails (payment successful)
8. ✉️ Payment failed alerts (billing issues)
9. ✉️ Document processing notifications (knowledge base updates)
10. ✉️ Ticket escalation alerts (support system)

**Configuration**:
```env
SMTP_HOST=smtp.zoho.com
SMTP_PORT=465                    # ✅ Updated
SMTP_TLS=true
SMTP_USER=noreply@luranai.com
SMTP_PASSWORD=T6R0pKBya9m8
EMAILS_FROM_EMAIL=noreply@luranai.com
EMAILS_FROM_NAME=Luran
```

**Requirements**:
- ✅ Must be a valid Zoho mailbox
- ✅ SMTP access must be enabled in Zoho
- ✅ Password must be correct (or app-specific password if 2FA enabled)

---

## Optional/Internal Emails (NOT used for sending)

### `admin@luranai.com`

**Purpose**: PgAdmin database management UI login

**Environment Variable**: `PGADMIN_EMAIL=admin@luranai.com`

**Notes**:
- Only used for logging into PgAdmin web interface
- NOT used for sending any emails
- Can be any email address (doesn't need to exist)

---

### `admin@example.com`

**Purpose**: Initial platform administrator account

**Environment Variable**: `FIRST_SUPERUSER_EMAIL=admin@example.com`

**Notes**:
- Used once during initial setup to create first super admin
- NOT used for sending any emails
- Should be changed to a real admin email in production

---

## Email Templates

All located in `app/templates/emails/`:

| File | Type | Trigger Event |
|------|------|---------------|
| `base.html` | Base template | All emails (wrapper) |
| `welcome.html` | Onboarding | New user account created |
| `user_invite.html` | Team | Team member invited to tenant |
| `password_reset.html` | Security | User clicks "Forgot password" |
| `password_changed.html` | Security | Password successfully changed |
| `email_verification.html` | Verification | New account needs email confirmation |
| `trial_expiring.html` | Billing | Trial ends in 7 days |
| `subscription_confirmed.html` | Billing | Paystack payment successful |
| `payment_failed.html` | Billing | Paystack payment failed |

---

## Email Service Implementation

### Two Implementations Available:

#### 1. `app/utils/email.py` (Basic)
- Uses `emails` Python library
- Simple configuration
- Works with Zoho

#### 2. `app/services/email_service.py` (Advanced) ⭐ RECOMMENDED
- Native Python `smtplib` and `ssl`
- Smart port detection:
  - **Port 465**: Uses `SMTP_SSL` (implicit TLS from start)
  - **Port 587**: Uses `SMTP` + `STARTTLS` (upgrade to TLS)
- Better error handling
- Template rendering with Jinja2

---

## Zoho Configuration Details

### SMTP Settings
- **Server**: `smtp.zoho.com`
- **Port**: `465` (SSL) or `587` (STARTTLS)
- **Security**: SSL/TLS Required
- **Authentication**: Username + Password

### Current Setup (Port 465 - SSL)
```
✅ SMTP_HOST=smtp.zoho.com
✅ SMTP_PORT=465
✅ SMTP_TLS=true
✅ SMTP_USER=noreply@luranai.com
✅ SMTP_PASSWORD=T6R0pKBya9m8
✅ EMAILS_FROM_EMAIL=noreply@luranai.com
✅ EMAILS_FROM_NAME=Luran
```

### Enable SMTP Access in Zoho
1. Login: https://mail.zoho.com
2. Settings → Mail Accounts → noreply@luranai.com
3. IMAP/POP Access → Enable SMTP

### If Using 2-Factor Authentication
1. Zoho Accounts → Security
2. Generate Application Password
3. Use app password instead of regular password

---

## Testing Email Configuration

### Quick Test
```bash
docker exec luran_api python3 << 'EOF'
from app.utils.email import send_email
import asyncio

async def test():
    result = await send_email(
        email_to="your@email.com",
        subject="Luran Email Test - Zoho Port 465",
        body="This confirms Zoho SMTP is working on port 465 (SSL).",
        html=False
    )
    print("✅ Success!" if result else "❌ Failed")

asyncio.run(test())
EOF
```

### Check Logs
```bash
docker logs luran_api | grep -i "email\|smtp" | tail -20
```

---

## Email Sending Limits

### Zoho Free Tier
- 500 emails/day
- 50 emails/hour
- 50 recipients/email

### Expected Usage
- Trial warnings: 10-50/day
- Subscription notifications: 5-20/day
- Password resets: 10-50/day
- Welcome emails: 5-20/day
- **Total**: ~50-150 emails/day ✅ Well within limits

---

## DNS Records (Improve Deliverability)

### SPF Record
```
Type: TXT
Name: @
Value: v=spf1 include:zoho.com ~all
```

### DKIM Record
Get from: Zoho Mail → Settings → Domains → Email Configuration → DKIM

### DMARC Record (Optional)
```
Type: TXT
Name: _dmarc
Value: v=DMARC1; p=none; rua=mailto:dmarc@luranai.com
```

---

## Troubleshooting

### Authentication Failed
- ✅ Check SMTP_USER = noreply@luranai.com (exact)
- ✅ Verify password is correct
- ✅ Use app password if 2FA enabled
- ✅ Enable SMTP in Zoho settings

### Connection Refused
- ✅ Port 465 (not 587)
- ✅ Firewall allows outbound port 465
- ✅ Docker can reach smtp.zoho.com

### Emails in Spam
- ✅ Set up SPF record
- ✅ Set up DKIM
- ✅ Verify domain in Zoho
- ✅ Use consistent sender info

---

## Summary Checklist

Configuration:
- [x] Port updated to 465 ✅
- [x] Zoho SMTP configured ✅
- [x] SSL/TLS enabled ✅
- [x] API container restarted ✅

Next Steps:
- [ ] Test email sending
- [ ] Verify emails arrive
- [ ] Check spam folder
- [ ] Set up SPF/DKIM (optional)

---

**ONE email address does it all: `noreply@luranai.com`** 📧
