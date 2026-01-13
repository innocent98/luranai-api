# Workflow 1: Document Ingestion - Detailed Explanation

## Your Two Questions Answered

### Question 1: Are we calling Gemini AI twice?

**Answer:** We're calling **TWO DIFFERENT Google APIs** at **THREE DIFFERENT TIMES**:

#### API Breakdown

| When | API Used | Model | Purpose | Input | Output |
|------|----------|-------|---------|-------|--------|
| **Upload Time** | Embedding API | embedding-001 | Convert chunks to vectors | "Q: What is covered?..." | [0.123, -0.456, ...] (768 numbers) |
| **Chat Time #1** | Embedding API | embedding-001 | Convert query to vector | "What is covered?" | [0.234, -0.567, ...] (768 numbers) |
| **Chat Time #2** | Gemini API | gemini-2.5-flash | Generate response | Full prompt + context | "Based on your policy..." (text) |

#### Important Distinctions

**Embedding API** (models/embedding-001)
- Purpose: Turn text into numbers (vectors)
- Fast: ~200ms per request
- Cheap: Free tier (1,500/day)
- No thinking: Just mathematical transformation
- Output: Always 768 numbers

**Gemini API** (gemini-2.5-flash)
- Purpose: Generate human-like text responses
- Slower: ~1-3 seconds per request
- More expensive: ~$0.075 per 1M tokens
- Thinks: Understands context and generates answers
- Output: Natural language text

#### Visual Flow

```
KNOWLEDGE UPLOAD
┌──────────────────────────────────────┐
│ Admin uploads document               │
│ "Q: What is covered? A: Theft..."   │
└──────────────┬───────────────────────┘
               │
               ▼
        [Chunk into pieces]
               │
               ▼
    ┌─────────────────────┐
    │  Embedding API #1   │  ← NOT Gemini!
    │  (embedding-001)    │
    │  Input: Chunk text  │
    │  Output: [numbers]  │
    └──────────┬──────────┘
               │
               ▼
     [Store in Qdrant + PostgreSQL]


USER CHATS
┌──────────────────────────────────────┐
│ User asks: "What is covered?"       │
└──────────────┬───────────────────────┘
               │
               ▼
    ┌─────────────────────┐
    │  Embedding API #2   │  ← NOT Gemini!
    │  (embedding-001)    │
    │  Input: User query  │
    │  Output: [numbers]  │
    └──────────┬──────────┘
               │
               ▼
      [Search Qdrant]
      Find similar vectors
               │
               ▼
      [Retrieve chunks]
      Get matching knowledge
               │
               ▼
    ┌─────────────────────┐
    │   Gemini API #3     │  ← This generates text!
    │  (gemini-2.5-flash) │
    │  Input: Prompt with │
    │         retrieved   │
    │         knowledge   │
    │  Output: Answer     │
    └──────────┬──────────┘
               │
               ▼
     [Return to user]
```

#### Cost Implications

**Per Document Upload (assume 10 chunks):**
- Embedding API calls: 10
- Gemini API calls: 0
- Cost: ~Free (within daily limit)

**Per Chat Message:**
- Embedding API calls: 1 (for query)
- Gemini API calls: 1 (for response)
- Cost: ~$0.0001 (if response is ~1,000 tokens)

**Daily Limits (Free Tier):**
- Embeddings: 1,500 requests/day
- Gemini: 15 requests/minute, 1,500/day
- If exceeded: Need paid API key

---

### Question 2: Should knowledge be uploaded via file?

**Answer:** YES! I just added proper file upload support.

## File Upload Options

### Option A: Direct File Upload (NEW! ✨)

**Endpoint:** `POST /api/v1/knowledge/upload-file`

**Supported Formats:**
- ✅ `.txt` - Plain text files
- ✅ `.md` - Markdown files
- ✅ `.pdf` - PDF documents (uses PyPDF2)
- ✅ `.docx` - Microsoft Word documents (uses python-docx)

**Example Usage:**

#### Using cURL
```bash
TOKEN="your-jwt-token"
TENANT_ID="bb24bf9e-63c2-4497-b5f5-e46dd916f7ea"

curl -X POST http://localhost:8000/api/v1/knowledge/upload-file \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/path/to/faq.txt" \
  -F "tenant_id=$TENANT_ID" \
  -F "source_type=faq"
```

#### Using Python
```python
import requests

# Login first
login_response = requests.post(
    "http://localhost:8000/api/v1/auth/login",
    json={"email": "admin@weysure.com", "password": "weysure123"}
)
token = login_response.json()["access_token"]

# Upload file
with open("faq.txt", "rb") as f:
    files = {"file": f}
    data = {
        "tenant_id": "bb24bf9e-63c2-4497-b5f5-e46dd916f7ea",
        "source_type": "faq"
    }
    headers = {"Authorization": f"Bearer {token}"}

    response = requests.post(
        "http://localhost:8000/api/v1/knowledge/upload-file",
        files=files,
        data=data,
        headers=headers
    )

    result = response.json()
    print(f"Uploaded: {result['name']}")
    print(f"Chunks: {result['chunk_count']}")
```

#### Using Swagger UI
1. Visit: http://localhost:8000/api/v1/docs
2. Find: `POST /api/v1/knowledge/upload-file`
3. Click "Try it out"
4. Click "Choose File" and select your document
5. Fill in tenant_id and source_type
6. Click "Execute"

**Advantages:**
- ✅ Easy to use (just upload file)
- ✅ Automatic text extraction
- ✅ Supports multiple formats
- ✅ Handles encoding automatically
- ✅ Better UX for end users

**File Size Limit:** 10MB

---

### Option B: Text Upload (Original)

**Endpoint:** `POST /api/v1/knowledge/upload`

**Use Case:** When you already have text content

```bash
TOKEN="your-jwt-token"

curl -X POST http://localhost:8000/api/v1/knowledge/upload \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "bb24bf9e-63c2-4497-b5f5-e46dd916f7ea",
    "document_name": "faq.txt",
    "document_text": "Q: What is covered?\nA: Theft, fire, natural disasters...",
    "source_type": "faq"
  }'
```

**When to Use:**
- ✅ Content generated programmatically
- ✅ API integrations (Zapier, webhooks)
- ✅ Scraped web content
- ✅ Testing with short snippets

---

## Complete Upload Flow with Details

### Step-by-Step with API Calls

```
1. USER UPLOADS FILE
   POST /api/v1/knowledge/upload-file
   File: faq.pdf (2 pages, 5KB)
        │
        ▼
2. BACKEND EXTRACTS TEXT
   import PyPDF2
   text = extract_text_from_pdf(file)
   Result: "Q: What is covered? A: ..."
        │
        ▼
3. COMPUTE HASH (SHA256)
   hash = sha256(text)
   Result: "a7b3c9d2e4f1..."
        │
        ▼
4. CHECK EXISTING
   SELECT * FROM knowledge_sources
   WHERE tenant_id = ? AND content_hash = ?

   Result: None found
   → Create new source (version 1)
        │
        ▼
5. CREATE SOURCE RECORD
   INSERT INTO knowledge_sources
   {
     id: "source-uuid",
     tenant_id: "tenant-uuid",
     name: "faq.pdf",
     version: 1,
     status: PROCESSING,
     chunk_count: 0
   }
        │
        ▼
6. CHUNK TEXT
   from app.utils.chunking import text_chunker

   chunks = text_chunker.chunk_text(
       text="Q: What is covered?...",
       metadata={'source': 'faq.pdf'}
   )

   Result: [
     {content: "Q: What is covered?...", chunk_index: 0},
     {content: "...natural disasters...", chunk_index: 1},
     {content: "...file a claim...", chunk_index: 2}
   ]
   Total: 3 chunks
        │
        ▼
7. GENERATE EMBEDDINGS (Google Embedding API)
   For each chunk:

   API Call #1:
   POST https://generativelanguage.googleapis.com/v1beta/models/embedding-001:embedContent
   {
     "content": "Q: What is covered?...",
     "taskType": "RETRIEVAL_DOCUMENT"
   }

   Response #1: {"embedding": [0.123, -0.456, 0.789, ... ]} (768 numbers)

   API Call #2: (same for chunk 2)
   API Call #3: (same for chunk 3)

   Total API calls: 3 (one per chunk)
        │
        ▼
8. STORE IN POSTGRESQL
   For each chunk:

   INSERT INTO knowledge_chunks
   {
     id: "chunk-uuid-1",
     tenant_id: "tenant-uuid",
     source_id: "source-uuid",
     content: "Q: What is covered?...",
     chunk_index: 0,
     vector_id: "source-uuid_0",
     is_active: true
   }

   Result: 3 rows in knowledge_chunks
        │
        ▼
9. STORE IN QDRANT
   For each chunk:

   POST http://localhost:6333/collections/luran_knowledge/points/upsert
   {
     "points": [{
       "id": "source-uuid_0",
       "vector": [0.123, -0.456, ...],  # 768 numbers
       "payload": {
         "tenant_id": "tenant-uuid",
         "chunk_id": "chunk-uuid-1",
         "content": "Q: What is covered?...",
         "source_id": "source-uuid"
       }
     }]
   }

   Result: 3 vectors in Qdrant
        │
        ▼
10. MARK SOURCE ACTIVE
    UPDATE knowledge_sources
    SET status = 'ACTIVE', chunk_count = 3
    WHERE id = 'source-uuid'
        │
        ▼
11. RETURN RESPONSE
    {
      "id": "source-uuid",
      "name": "faq.pdf",
      "chunk_count": 3,
      "status": "ACTIVE",
      "version": 1
    }
```

---

## Verification

### Check in PostgreSQL
```sql
-- View uploaded source
SELECT * FROM knowledge_sources
WHERE name = 'faq.pdf';

-- View chunks
SELECT id, chunk_index, LEFT(content, 50) as preview
FROM knowledge_chunks
WHERE source_id = 'source-uuid'
ORDER BY chunk_index;
```

### Check in Qdrant
```bash
# Count vectors
curl http://localhost:6333/collections/luran_knowledge | jq '.result.points_count'

# View vectors for tenant
curl -X POST http://localhost:6333/collections/luran_knowledge/points/scroll \
  -H "Content-Type: application/json" \
  -d '{
    "filter": {
      "must": [
        {"key": "tenant_id", "match": {"value": "tenant-uuid"}}
      ]
    },
    "limit": 10,
    "with_payload": true
  }' | jq
```

---

## Summary

### API Usage Clarification

1. **Embedding API** (models/embedding-001)
   - Used during upload: YES (for each chunk)
   - Used during chat: YES (for user query)
   - Purpose: Convert text to vectors
   - This is NOT Gemini

2. **Gemini API** (gemini-2.5-flash)
   - Used during upload: NO
   - Used during chat: YES (for generating response)
   - Purpose: Generate natural language answers
   - This is the "thinking" AI

### File Upload Support

✅ **NOW AVAILABLE:** Direct file upload via `/api/v1/knowledge/upload-file`

**Supported:**
- Text files (.txt, .md)
- PDFs (.pdf)
- Word documents (.docx)

**Max size:** 10MB per file

**Try it:** Visit http://localhost:8000/api/v1/docs and look for "upload-file"

---

## Common Questions

**Q: Why separate embedding and generation APIs?**
A: Specialization - embedding model is optimized for semantic similarity, Gemini is optimized for text generation.

**Q: Can I reduce API costs?**
A: Yes - cache common queries, batch process uploads, use smaller models for non-critical responses.

**Q: What if embeddings fail during upload?**
A: Chunks are created but marked as failed. Document status = FAILED. No vectors in Qdrant.

**Q: Can I upload without embeddings?**
A: Technically yes (chunks stored) but search won't work. AI won't find relevant knowledge.

**Q: Does file upload work with all file types?**
A: Currently: txt, md, pdf, docx. Can extend to: html, csv, json, xlsx with additional parsers.
