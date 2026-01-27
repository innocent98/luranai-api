# Paystack Recurring Subscription Setup Guide

This guide explains how to configure Paystack for recurring subscription payments in Luran.

## Prerequisites

1. Paystack account (sign up at https://paystack.com)
2. Business verified (required for subscriptions)
3. Access to Paystack Dashboard

---

## Step 1: Get Your API Keys

1. Log in to Paystack Dashboard
2. Go to **Settings** → **Developers** (https://dashboard.paystack.com/#/settings/developers)
3. Copy your keys:
   - **Test Secret Key** (starts with `sk_test_`) - for development
   - **Test Public Key** (starts with `pk_test_`) - for frontend
   - **Live Secret Key** (starts with `sk_live_`) - for production
   - **Live Public Key** (starts with `pk_live_`) - for frontend

4. Add to your `.env` file:
   ```bash
   PAYSTACK_SECRET_KEY=sk_test_your_secret_key_here
   PAYSTACK_PUBLIC_KEY=pk_test_your_public_key_here
   ```

---

## Step 2: Create Subscription Plans

Luran requires 4 subscription plans to be created in your Paystack dashboard.

### Go to Plans Page
Navigate to: **Transactions** → **Plans** (https://dashboard.paystack.com/plans)

Click **Create Plan** button.

### Plan 1: Luran Lite Monthly

- **Plan Name**: `Luran Lite Monthly`
- **Plan Code**: `PLN_lite_monthly`
- **Amount**: `12000` (₦12,000)
- **Interval**: `Monthly`
- **Currency**: `NGN`
- **Send invoices**: `Yes`
- **Description**: Monthly subscription to Luran Lite tier

Click **Create Plan**

### Plan 2: Luran Lite Annual

- **Plan Name**: `Luran Lite Annual`
- **Plan Code**: `PLN_lite_annual`
- **Amount**: `120000` (₦120,000)
- **Interval**: `Annually`
- **Currency**: `NGN`
- **Send invoices**: `Yes`
- **Description**: Annual subscription to Luran Lite tier (save 2 months)

Click **Create Plan**

### Plan 3: Luran Professional Monthly

- **Plan Name**: `Luran Professional Monthly`
- **Plan Code**: `PLN_professional_monthly`
- **Amount**: `35000` (₦35,000)
- **Interval**: `Monthly`
- **Currency**: `NGN`
- **Send invoices**: `Yes`
- **Description**: Monthly subscription to Luran Professional tier

Click **Create Plan**

### Plan 4: Luran Professional Annual

- **Plan Name**: `Luran Professional Annual`
- **Plan Code**: `PLN_professional_annual`
- **Amount**: `350000` (₦350,000)
- **Interval**: `Annually`
- **Currency**: `NGN`
- **Send invoices**: `Yes`
- **Description**: Annual subscription to Luran Professional tier (save 2 months)

Click **Create Plan**

### Verify Plans Created

You should now see 4 plans in your Plans page. Make sure the **Plan Codes** match exactly:
- `PLN_lite_monthly`
- `PLN_lite_annual`
- `PLN_professional_monthly`
- `PLN_professional_annual`

**IMPORTANT**: The plan codes must match exactly as they are hardcoded in the application.

---

## Step 3: Configure Webhook

Webhooks allow Paystack to notify your server about payment events (successful payments, failed renewals, etc.).

### Go to Webhooks Settings
Navigate to: **Settings** → **Webhooks** (https://dashboard.paystack.com/#/settings/webhooks)

### Add Webhook URL

1. Click **Add Webhook URL**
2. Enter your webhook URL:
   - **Development**: `https://your-dev-domain.com/api/v1/webhooks/paystack`
   - **Production**: `https://luran.ai/api/v1/webhooks/paystack`

   **Note**: Paystack requires HTTPS. For local development, use ngrok or similar.

3. Click **Add URL**

### Copy Webhook Secret

After adding the URL, Paystack will generate a webhook secret:

1. Click on the webhook URL you just added
2. Copy the **Secret Key**
3. Add to your `.env` file:
   ```bash
   PAYSTACK_WEBHOOK_SECRET=your_webhook_secret_here
   ```

### Select Events to Listen For

In the webhook configuration, select these events:
- ✅ `charge.success` - First payment successful
- ✅ `subscription.create` - Subscription created
- ✅ `subscription.disable` - Subscription canceled
- ✅ `subscription.not_renew` - Subscription set to not renew
- ✅ `invoice.create` - Invoice created for renewal
- ✅ `invoice.payment_failed` - Renewal payment failed

Click **Save Changes**

---

## Step 4: Test Webhook

### Test Webhook Health Check

Run this command to verify your webhook endpoint is accessible:

```bash
curl https://your-domain.com/api/v1/webhooks/paystack/health
```

Expected response:
```json
{
  "status": "ok",
  "message": "Webhook endpoint is configured and ready to receive events"
}
```

### Test Webhook from Paystack Dashboard

1. Go to your webhook settings
2. Click on your webhook URL
3. Click **Test Webhook**
4. Select event type: `charge.success`
5. Click **Send Test**
6. Check your server logs to see if event was received

---

## Step 5: Test Payment Flow

### Using Paystack Test Cards

Paystack provides test cards for development:

#### Successful Payment
- **Card Number**: `4084084084084081`
- **Expiry**: Any future date (e.g., `12/25`)
- **CVV**: `408`
- **OTP**: `123456`

#### Failed Payment
- **Card Number**: `5060666666666666666`
- **Expiry**: Any future date
- **CVV**: `123`

### Complete Test Flow

1. **Initialize Payment**:
   ```bash
   curl -X POST https://your-domain.com/api/v1/subscription/upgrade/initialize \
     -H "Authorization: Bearer YOUR_JWT_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{
       "tier": "lite",
       "billing_cycle": "monthly"
     }'
   ```

2. **Response**:
   ```json
   {
     "authorization_url": "https://checkout.paystack.com/...",
     "access_code": "xxx",
     "reference": "luran_sub_71ca7137_abc123",
     "amount": 1200000,
     "currency": "NGN",
     "plan_code": "PLN_lite_monthly"
   }
   ```

3. **Open Payment Page**: Visit the `authorization_url` in your browser

4. **Complete Payment**: Use test card details above

5. **Verify Webhooks**: Check your server logs for:
   - `charge.success` event
   - `subscription.create` event

6. **Verify Subscription Upgraded**:
   ```bash
   curl https://your-domain.com/api/v1/tenant \
     -H "Authorization: Bearer YOUR_JWT_TOKEN"
   ```

   Check that `subscription_tier` is now `lite` and `subscription_status` is `active`.

---

## Step 6: Monitor Recurring Payments

Once subscriptions are active, Paystack will automatically charge customers on renewal dates.

### What Happens on Renewal

**7 days before renewal**:
- Paystack sends reminder email to customer

**On renewal date**:
1. Paystack creates invoice (`invoice.create` webhook)
2. Paystack attempts to charge saved card
3. If successful: `charge.success` webhook sent
4. If failed: `invoice.payment_failed` webhook sent
5. Paystack retries failed payments for 3 days

### Check Subscription Status

View subscription details in Paystack Dashboard:
- Go to **Transactions** → **Subscriptions**
- Search by customer email
- View subscription history and next billing date

### Handle Failed Payments

When payment fails:
1. Webhook sets tenant status to `past_due`
2. Tenant can still access platform for grace period
3. Send notification email to admin (TODO: implement)
4. After 3 failed retries, Paystack cancels subscription
5. Webhook sets tenant status to `canceled`

---

## Step 7: Production Checklist

Before going live with production keys:

- [ ] All 4 subscription plans created in production
- [ ] Production webhook URL configured
- [ ] Webhook secret added to production `.env`
- [ ] Tested successful payment flow
- [ ] Tested failed payment handling
- [ ] Tested subscription cancellation
- [ ] SSL certificate installed (HTTPS required)
- [ ] Business verified on Paystack
- [ ] Bank account added for settlements

---

## Troubleshooting

### Webhook Not Receiving Events

1. **Check webhook URL is accessible**:
   ```bash
   curl https://your-domain.com/api/v1/webhooks/paystack/health
   ```

2. **Check webhook signature**: Ensure `PAYSTACK_WEBHOOK_SECRET` is correctly set in `.env`

3. **Check Paystack logs**: Go to Dashboard → Webhooks → Click your URL → View logs

4. **Verify HTTPS**: Paystack only sends webhooks to HTTPS URLs

### Plan Not Found Error

Error: `No plan code for lite monthly`

**Solution**: Verify plan codes in Paystack dashboard match exactly:
- `PLN_lite_monthly`
- `PLN_lite_annual`
- `PLN_professional_monthly`
- `PLN_professional_annual`

### Subscription Not Upgrading

Check webhook logs for `charge.success` event. If event received but subscription not upgraded:

1. Check server logs for errors
2. Verify transaction record was created
3. Check tenant subscription status in database
4. Manually trigger upgrade if needed

### Failed Renewal Not Detected

Check webhook logs for `invoice.payment_failed` event. Ensure:
1. Webhook is receiving the event
2. `subscription_code` is stored in transaction record
3. Tenant status updates to `past_due`

---

## Support

For Paystack-specific issues:
- Documentation: https://paystack.com/docs
- Support: support@paystack.com
- Community: https://paystack.com/community

For Luran integration issues:
- Check server logs for errors
- Review webhook event data in Paystack dashboard
- Contact development team

---

## Summary

✅ API keys configured
✅ 4 subscription plans created
✅ Webhook URL configured
✅ Webhook events selected
✅ Test payment successful
✅ Recurring billing active

Your Paystack integration is now complete and ready for recurring subscription payments!
