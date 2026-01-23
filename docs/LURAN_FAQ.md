# Luran - Frequently Asked Questions (FAQ)

**Last Updated**: January 23, 2026
**Version**: 1.0

Welcome to Luran! This FAQ covers everything you need to know about our AI-powered customer support platform.

---

## Table of Contents

1. [General Questions](#general-questions)
2. [For Businesses](#for-businesses)
3. [For End Users (Customers)](#for-end-users-customers)
4. [How the AI Works](#how-the-ai-works)
5. [Knowledge Base & Training](#knowledge-base--training)
6. [Escalation & Human Support](#escalation--human-support)
7. [Privacy & Security](#privacy--security)
8. [Widget & Integration](#widget--integration)
9. [Pricing & Plans](#pricing--plans)
10. [Technical Questions](#technical-questions)

---

## General Questions

### What is Luran?

Luran is an intelligent customer support platform that combines AI-powered chatbots with human agent escalation. It uses advanced AI (Google Gemini) to answer customer questions based on your company's knowledge base, and seamlessly escalates to human support when needed.

### How is Luran different from other chatbots?

Unlike traditional chatbots that use scripted responses or simple keyword matching, Luran:

- **Understands context** using Retrieval-Augmented Generation (RAG)
- **Learns from your documents** - answers come from YOUR knowledge base, not generic training data
- **Knows when to escalate** - automatically detects when it can't help and connects customers to humans
- **Never hallucinates** - only provides answers it can verify from your documents
- **Works in real-time** - seamless handoff to live agents via WebSocket

### What does RAG (Retrieval-Augmented Generation) mean?

RAG is an AI technique that:

1. **Searches your knowledge base** for relevant information (Retrieval)
2. **Uses AI to understand and synthesize** the information (Augmentation)
3. **Generates natural responses** based only on what it found (Generation)

This means Luran's AI doesn't "make things up" - it only answers based on your actual documentation.

### Who is Luran for?

Luran is perfect for:

- **E-commerce businesses** - Product questions, order status, returns
- **SaaS companies** - Technical support, feature questions, billing
- **Insurance companies** - Policy questions, claims assistance
- **Healthcare providers** - Appointment scheduling, insurance verification
- **Any business** with a knowledge base and customer support needs

---

## For Businesses

### How do I get started with Luran?

1. **Register your company** - Sign up with your business email
2. **Upload your knowledge base** - Add FAQs, product docs, policies (PDF, DOCX, TXT, Markdown)
3. **Install the widget** - Add one line of code to your website
4. **Test and customize** - Adjust colors, behavior, and escalation rules
5. **Go live** - Start helping customers 24/7

The entire setup takes 15-30 minutes.

### What types of documents can I upload?

Luran supports:

- **PDF** - Manuals, guides, policies
- **DOCX** - Word documents
- **TXT** - Plain text files
- **Markdown** - .md files
- **Web content** - Copy/paste from your help center

Each document is automatically processed, split into searchable chunks, and indexed for instant retrieval.

### How many documents can I upload?

There's no hard limit on document count. The system is designed to handle:

- Small businesses: 10-50 documents
- Medium businesses: 100-500 documents
- Enterprise: 1,000+ documents

Performance remains fast even with large knowledge bases thanks to vector search.

### Can I update my knowledge base after going live?

Yes! You can:

- **Upload new documents** anytime
- **Update existing documents** (creates a new version automatically)
- **Delete outdated information**
- **Mark documents as inactive** without deleting them

Changes are processed in the background and become available within minutes.

### How do I install the widget on my website?

Add this code snippet before the closing `</body>` tag on your website:

```html
<script>
  window.LuranConfig = {
    tenantId: 'your-tenant-id'
  };
</script>
<script src="https://cdn.luranai.com/widget/v1/widget.js" async></script>
```

That's it! The widget will appear as a floating chat button on the bottom-right corner.

### Can I customize the widget appearance?

Yes! You can customize:

- **Brand colors** - Primary color, button color, text colors
- **Position** - Bottom-right, bottom-left, or custom
- **Widget title** - Company name and greeting
- **Button text** - "Chat with us", "Need help?", etc.
- **Welcome message** - First message customers see

All settings are managed from your Luran dashboard - no code changes needed.

### How much does Luran cost?

Pricing is based on your usage:

- **Message volume** - Number of AI responses per month
- **Knowledge base size** - Amount of content indexed
- **Support agents** - Number of human agents

Contact our sales team for current pricing or visit our pricing page.

_(Note: This is placeholder text - update with actual pricing when available)_

### Is there a free trial?

Yes! New businesses get:

- **14-day free trial**
- **Full feature access** - All features unlocked
- **No credit card required** - Cancel anytime
- **Onboarding support** - Help with setup

---

## For End Users (Customers)

### How do I use the chat widget?

1. **Click the chat button** on the bottom-right of the website
2. **Type your question** - Ask anything about the company's products/services
3. **Get instant answers** - The AI responds within 2-5 seconds
4. **Continue the conversation** - Ask follow-up questions

Your conversation is saved, so you can close the widget and return anytime.

### Is a human helping me or a robot?

Initially, you're chatting with an **AI assistant** powered by Google Gemini. The AI:

- Answers based on the company's knowledge base
- Responds in natural, conversational language
- Can handle multiple questions in the same conversation

If the AI can't help, it will **offer to connect you with a human agent**.

### How do I talk to a real person?

There are three ways:

1. **Ask directly** - Say "I want to speak to a human" or "Can I talk to an agent?"
2. **AI offers escalation** - If the AI can't answer, it asks: "Would you like me to connect you with our team?"
3. **Say yes** - When offered, respond "yes" or "sure"

Once escalated, a support agent will join your conversation within minutes.

### How will I know when a human agent joins?

You'll see:

- **Notification message** - "John Smith joined the conversation"
- **Profile picture** - Agent's name and photo appear
- **Real-time typing** - You'll see "John is typing..." indicators

From that point forward, all messages come from the human agent.

### Can I go back to the AI after talking to a human?

Once escalated to a human agent, that conversation remains with the agent. However:

- You can start a **new conversation** in the widget (new session)
- The new conversation will start with the AI again
- Your previous conversation with the agent remains accessible

### What if I close the chat and come back later?

Your conversation is automatically saved! When you return:

- **Same session continues** - All your messages are still there
- **AI remembers context** - It knows what you discussed
- **No need to repeat yourself** - Pick up where you left off

Sessions remain active for 30+ days.

### Is my conversation private?

Yes! Your conversations are:

- **Encrypted in transit** (HTTPS)
- **Stored securely** in the company's tenant-isolated database
- **Only visible to** the company's support team
- **Never shared** with other businesses

Luran uses strict multi-tenant security to keep your data separate.

---

## How the AI Works

### How does the AI know the answers?

The AI doesn't have pre-programmed answers. Instead, it:

1. **Searches the knowledge base** when you ask a question
2. **Finds the most relevant documents** using semantic search (understands meaning, not just keywords)
3. **Reads those documents** in real-time
4. **Generates a natural answer** based only on what it found

If it doesn't find relevant information, it says "I don't have information about that" instead of guessing.

### What is "semantic search"?

Unlike traditional keyword search (matching exact words), semantic search understands **meaning**:

- "How do I return an item?" matches "refund policy" even without the word "return"
- "What's your cancellation policy?" matches "how to cancel subscription"
- "Do you ship internationally?" matches "worldwide delivery options"

This makes the AI much better at finding the right answer.

### Does the AI learn from conversations?

Currently, the AI does **not** learn from customer conversations. It only learns from:

- **Uploaded documents** in the knowledge base
- **Updates to existing documents**

This is intentional to ensure accuracy - the AI only uses verified, approved information.

_(Note: Conversation-based learning may be added in future versions with admin approval)_

### Can the AI answer questions in multiple languages?

The AI can understand and respond in multiple languages, but performance is best when:

- **Knowledge base is in the same language** as the customer question
- **Mixed-language support** depends on how documents are uploaded

For best results, upload documents in the primary language(s) your customers use.

### How accurate is the AI?

Luran's AI is highly accurate because:

- **No hallucinations** - Only answers from verified documents
- **Similarity threshold** - Requires 50% confidence match before answering
- **Quality checks** - Evaluates response quality before sending
- **Smart escalation** - If uncertain, escalates to humans

In testing, Luran achieves:

- **~90% answer accuracy** for questions within the knowledge base
- **~95% escalation precision** (correctly identifies when to escalate)

### What happens if the AI doesn't know the answer?

The AI will:

1. **Say honestly** "I don't have information about that"
2. **Offer escalation** "Would you like me to connect you with our team?"
3. **Wait for your consent** before escalating

It will **never** guess or make up answers.

---

## Knowledge Base & Training

### How long does it take to process uploaded documents?

Processing time depends on document size:

- **Small documents** (1-10 pages): 30 seconds - 2 minutes
- **Medium documents** (10-50 pages): 2-5 minutes
- **Large documents** (50+ pages): 5-15 minutes

You'll see real-time progress updates in the dashboard.

### How does Luran split documents into chunks?

Documents are intelligently split into **searchable chunks** (~600 characters each) using:

- **Sentence boundaries** - Doesn't cut mid-sentence
- **Semantic coherence** - Keeps related information together
- **Overlap** - Small overlap between chunks to preserve context

This ensures the AI can find precise answers without losing context.

### Can I control which documents the AI uses?

Yes! Each document has:

- **Visibility levels**:
  - **Public** - Available to all customers
  - **Internal** - Only visible to your team (not used in customer chat)
  - **Restricted** - Requires special permissions

- **Active/Inactive status** - Turn documents on/off without deleting them

### What if I have contradictory information in different documents?

The AI uses the **most recent and relevant** information:

- **Version control** - Newer versions override older ones
- **Similarity ranking** - Uses the document with highest relevance score
- **Recency preference** - When similarity is equal, uses newer document

**Best practice**: Remove or deactivate outdated documents to avoid confusion.

### Can I see which documents the AI used for each answer?

Yes! Each AI response includes:

- **Source attribution** (visible in message metadata)
- **Chunk IDs** - References to specific knowledge chunks used
- **Similarity scores** - How confident the AI was

This is visible to your support team for quality assurance.

---

## Escalation & Human Support

### When does the AI escalate to humans?

Escalation happens in three scenarios:

1. **Auto-escalate** (immediate):
   - Customer says "I want to speak to a human"
   - Critical keywords detected: "lawsuit", "complaint", "cancel subscription"
   - Satisfaction + human request: "Thanks! Can I also talk to someone?"

2. **Offer escalation** (asks first):
   - No relevant knowledge found
   - Low similarity scores (< 50% confidence)
   - Weak/uncertain AI response quality

3. **No escalation**:
   - High-quality answer found
   - Customer expresses satisfaction ("Thanks!", "That helps!")

### How does consent-based escalation work?

When the AI **offers** to escalate:

1. **AI asks**: "I don't have information about that. Would you like me to connect you with our team?"
2. **Customer responds**:
   - "Yes" / "Sure" / "Please" → **Escalates** to human
   - "No" / "Nope" / "I'm good" → **Continues** with AI
   - Unclear response → **Treats as new question**, continues with AI

3. **If yes**: Creates support ticket, notifies agents, customer waits for agent to join

This ensures customers are never forced to talk to humans if they prefer self-service.

### How long does it take for a human to respond after escalation?

Response time depends on:

- **Agent availability** - During business hours vs after hours
- **Ticket priority** - Urgent tickets get faster response
- **Queue depth** - Number of open tickets

Typical response times:

- **Urgent tickets**: 5-15 minutes
- **High priority**: 15-30 minutes
- **Medium priority**: 30 minutes - 2 hours
- **Low priority**: 2-4 hours

_(Note: These are example SLAs - each business sets their own)_

### What is ticket priority and how is it determined?

Tickets are automatically assigned priority based on:

**URGENT** - Critical keywords detected:
- "urgent", "emergency", "critical", "asap"
- "down", "broken", "not working", "can't access"
- "losing money", "losing customers"

**HIGH** - User explicitly requested human OR critical escalation keyword

**MEDIUM** - No relevant knowledge found, general escalation

**LOW** - Response quality issues, minor concerns

Support agents can adjust priority after reviewing the conversation.

### Can multiple agents handle the same conversation?

Yes! Luran supports:

- **Multiple agents monitoring** the same session via WebSocket
- **One active agent** responding at a time
- **Agent handoff** - Agents can transfer conversations
- **Supervisor view** - Admins can oversee all conversations

Customers always see who is currently helping them.

### What happens after a ticket is resolved?

When an agent marks a ticket as resolved:

1. **Session status** changes to `resolved`
2. **Customer can still view** the conversation history
3. **AI doesn't respond** in that session anymore
4. **Customer can start a new conversation** anytime

Resolved conversations are archived but remain accessible for reference.

---

## Privacy & Security

### Is my data secure?

Yes! Luran uses enterprise-grade security:

- **Encryption in transit** - All data sent over HTTPS/TLS
- **Encryption at rest** - Database encryption (PostgreSQL)
- **Multi-tenant isolation** - Your data is completely separate from other businesses
- **Role-based access** - Only authorized team members can access data
- **Regular backups** - Automated daily backups with 30-day retention

### Who can see customer conversations?

Access is strictly controlled:

- **Your support team** - Agents and admins in your organization
- **Not shared** - Never visible to other Luran customers
- **Not used for AI training** - Conversations are NOT used to train models
- **Audit logs** - Track who accessed what data

### Does Luran sell or share customer data?

**Absolutely not.** Luran:

- **Never sells data** to third parties
- **Never shares data** with other businesses
- **Never uses conversations** for marketing or analytics outside your org
- **Only processes data** to provide the service

Your data is yours. Period.

### Is Luran GDPR compliant?

Yes! Luran supports GDPR requirements:

- **Right to access** - Export customer data on request
- **Right to deletion** - Delete customer data permanently
- **Data portability** - Export in standard formats (JSON, CSV)
- **Consent management** - Clear opt-in for data collection
- **Data processing agreements** - Available for enterprise customers

### How long is conversation data stored?

- **Active conversations** - Retained indefinitely
- **Resolved tickets** - Retained for 2 years (configurable)
- **Deleted data** - Permanently removed within 30 days
- **Backups** - Encrypted backups retained for 30 days

Businesses can configure their own retention policies.

### Can customers request data deletion?

Yes! Under GDPR and privacy laws, customers can:

1. **Request access** to their conversation data
2. **Request deletion** of their data
3. **Opt-out** of data collection

Businesses handle these requests through the Luran dashboard.

---

## Widget & Integration

### Does the widget slow down my website?

No! The Luran widget is optimized for performance:

- **Async loading** - Doesn't block page load
- **Lazy initialization** - Only loads when needed
- **Small footprint** - ~10KB gzipped (smaller than most images)
- **CDN delivery** - Fast global content delivery
- **No dependencies** - Pure JavaScript, no jQuery or React

Independent tests show **< 0.1 second** impact on page load time.

### Will the widget work on mobile devices?

Yes! The widget is fully responsive:

- **Mobile-optimized** - Touch-friendly interface
- **Adaptive layout** - Adjusts to screen size
- **Portrait/landscape** - Works in both orientations
- **iOS and Android** - Tested on major browsers

On mobile, the chat window expands to full-screen for better readability.

### Can I use Luran without the widget?

Yes! Luran offers multiple integration options:

- **REST API** - Build your own chat interface
- **WebSocket API** - Real-time messaging
- **Mobile SDKs** - Native iOS/Android apps (coming soon)
- **Slack integration** - Answer support requests in Slack (coming soon)

The widget is just the easiest way to get started.

### Does the widget work with single-page apps (React, Vue, Angular)?

Yes! The widget is framework-agnostic and works with:

- **React** - useState, useEffect compatible
- **Vue** - Vue 2 and Vue 3
- **Angular** - All versions
- **Next.js** - SSR and CSR
- **Vanilla JS** - No framework needed

Just add the script tag and it works.

### Can I have different widget configurations for different pages?

Yes! You can:

- **Conditional loading** - Load widget only on specific pages
- **Page-specific metadata** - Pass page URL, product ID, etc.
- **Dynamic configuration** - Change colors/text based on page
- **A/B testing** - Show different variants to different users

All through JavaScript configuration before loading the widget.

### How do I track widget analytics?

Luran provides built-in analytics:

- **Conversation metrics** - Total conversations, resolution rate
- **Response time** - AI response time, agent response time
- **Escalation rate** - How often AI escalates to humans
- **Knowledge gaps** - Questions AI couldn't answer
- **Agent performance** - Tickets resolved, average handle time

Accessible via the Luran dashboard.

---

## Pricing & Plans

### How is usage calculated?

Luran meters:

- **AI messages** - Each AI response counts as 1 message
- **Knowledge base storage** - Total size of uploaded documents (MB)
- **Active agents** - Number of support agents with access

**Free items**:
- Customer messages (inbound) - unlimited
- Conversation sessions - unlimited
- API calls - unlimited
- Widget impressions - unlimited

### Are there overage charges?

Overage pricing varies by plan:

- **Starter** - Pay-as-you-go for overages
- **Professional** - Reduced overage rates
- **Enterprise** - Custom pricing, no overages

You'll receive alerts when approaching limits.

### Can I change plans mid-month?

Yes! Plan changes:

- **Upgrades** - Take effect immediately, prorated billing
- **Downgrades** - Take effect at next billing cycle
- **Free trial** - Can upgrade anytime, trial period ends

No penalties for changing plans.

### Is there a long-term contract?

- **Starter and Professional** - Month-to-month, cancel anytime
- **Enterprise** - Typically annual contract with discounts

No long-term commitment required for standard plans.

### What payment methods do you accept?

- **Credit cards** - Visa, MasterCard, Amex, Discover
- **ACH/bank transfer** - For Enterprise plans
- **Invoicing** - Available for annual Enterprise contracts

Payments are processed securely through Stripe.

---

## Technical Questions

### What AI model does Luran use?

Luran uses **Google Gemini 1.5 Flash** for:

- **Fast responses** - 2-5 second latency
- **High accuracy** - State-of-the-art language understanding
- **Cost efficiency** - Optimized token usage

We continuously evaluate new models to improve performance.

### What is vector search?

Vector search (also called semantic search) works by:

1. **Converting text to numbers** (embeddings) - Documents and queries become vectors
2. **Measuring similarity** - Compares vectors mathematically
3. **Finding closest matches** - Returns most similar documents

This allows finding "car insurance" when customer asks "auto coverage" even though words differ.

### What is Qdrant?

Qdrant is a **vector database** - specialized for storing and searching embeddings. It:

- **Indexes millions of vectors** in milliseconds
- **Handles complex filters** (tenant isolation, visibility, status)
- **Scales horizontally** - Add more servers as you grow

Luran uses Qdrant for all knowledge base searches.

### How does multi-tenancy work?

Every piece of data is tagged with a `tenant_id`:

- **Database level** - Tenant ID in all queries
- **Vector search** - Tenant ID in Qdrant filters
- **API level** - Tenant ID validated on every request
- **WebSocket** - Tenant ID checked for connections

This ensures Company A can **never** see Company B's data, even by mistake.

### What happens if the AI service goes down?

Luran has multiple fallback mechanisms:

- **Circuit breaker** - Detects API failures, stops sending requests
- **Rate limiting** - Prevents overload (30 requests/min)
- **Graceful degradation** - Auto-escalates to humans if AI is down
- **Error messages** - Clear user feedback if system is unavailable

Uptime SLA: 99.9% for Enterprise customers.

### Does Luran have an API?

Yes! Luran provides:

- **REST API** - Full CRUD operations
- **WebSocket API** - Real-time messaging
- **Webhook support** - Event notifications (coming soon)
- **API documentation** - OpenAPI/Swagger spec

API access is available on all plans.

### Can I self-host Luran?

Currently, Luran is available as:

- **Cloud SaaS** - Fully managed, no infrastructure needed

**Enterprise self-hosting** (on-premises) is:

- **Coming Q2 2026** - Docker-based deployment
- **Kubernetes support** - For large enterprises
- **Custom pricing** - Contact sales

### What browsers are supported?

**Widget compatibility**:

- **Chrome** - Version 90+
- **Firefox** - Version 88+
- **Safari** - Version 14+
- **Edge** - Version 90+
- **Mobile browsers** - iOS Safari, Chrome Android

**Dashboard compatibility** (for businesses):

- **Chrome** - Version 100+ (recommended)
- **Firefox** - Version 100+
- **Safari** - Version 15+
- **Edge** - Version 100+

### Does Luran have WebSocket support?

Yes! WebSocket is used for:

- **Real-time agent chat** - Instant message delivery
- **Typing indicators** - "Agent is typing..."
- **Presence updates** - Agent joined/left notifications
- **Live updates** - Ticket status changes

WebSocket is optional - REST API works standalone too.

---

## Still Have Questions?

### Contact Support

- **Email**: support@luranai.com
- **Live Chat**: Available on our website
- **Documentation**: https://docs.luranai.com
- **Status Page**: https://status.luranai.com

### Community

- **GitHub**: https://github.com/luranai (coming soon)
- **Discord**: Join our community (link in dashboard)
- **Blog**: https://blog.luranai.com

### Sales & Partnerships

- **Sales**: sales@luranai.com
- **Partnerships**: partners@luranai.com
- **Enterprise**: enterprise@luranai.com

---

**Last Updated**: January 23, 2026
**Version**: 1.0
**Feedback**: help us improve this FAQ at feedback@luranai.com
