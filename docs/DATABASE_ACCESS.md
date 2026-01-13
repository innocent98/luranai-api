# Database and Vector Store Access Guide

## 1. 🗄️ PostgreSQL Database Access

### Connection Details
```
Host: localhost
Port: 5432
Database: luran_db
Username: user
Password: password
Connection String: postgresql://user:password@localhost:5432/luran_db
```

### Method A: Command Line (psql)

**Connect to database:**
```bash
# Direct connection
psql -h localhost -p 5432 -U user -d luran_db

# Using connection string
psql postgresql://user:password@localhost:5432/luran_db

# Via Docker
docker exec -it luran_db psql -U user -d luran_db
```

**Useful SQL Commands:**
```sql
-- List all tables
\dt

-- Describe a table
\d tenants
\d knowledge_sources
\d messages

-- View all tenants
SELECT id, name, display_name, status FROM tenants;

-- View knowledge sources
SELECT id, name, source_type, chunk_count, status, created_at
FROM knowledge_sources;

-- View conversation sessions
SELECT id, tenant_id, user_identifier, status, created_at
FROM conversation_sessions
ORDER BY created_at DESC
LIMIT 10;

-- View messages in a session
SELECT id, role, content, confidence_score, created_at
FROM messages
WHERE session_id = 'your-session-id'
ORDER BY created_at;

-- View tickets
SELECT id, ticket_number, status, priority, summary, created_at
FROM tickets
ORDER BY created_at DESC;

-- Count chunks by tenant
SELECT tenant_id, COUNT(*) as chunk_count
FROM knowledge_chunks
WHERE is_active = true
GROUP BY tenant_id;

-- Get AI config for tenant
SELECT * FROM tenant_ai_configs
WHERE tenant_id = 'bb24bf9e-63c2-4497-b5f5-e46dd916f7ea';
```

### Method B: GUI Tools

#### DBeaver (Recommended - Free)

1. Download: https://dbeaver.io/download/
2. Create new connection → PostgreSQL
3. Enter connection details above
4. Test connection → Finish

**Features:**
- Visual query builder
- ER diagrams
- Data export/import
- SQL editor with autocomplete

#### pgAdmin (Web-based)

1. Download: https://www.pgadmin.org/download/
2. Add new server
3. Connection tab → Enter details above

#### TablePlus (Mac - Paid)

1. Download: https://tableplus.com/
2. Create new connection → PostgreSQL
3. Enter details

#### DataGrip (JetBrains - Paid)

Professional database IDE with advanced features.

### Method C: Python Script

```python
import psycopg2
from psycopg2.extras import RealDictCursor

# Connect
conn = psycopg2.connect(
    host="localhost",
    port=5432,
    database="luran_db",
    user="user",
    password="password"
)

# Query with dictionary cursor
cursor = conn.cursor(cursor_factory=RealDictCursor)

# Get all tenants
cursor.execute("SELECT * FROM tenants;")
tenants = cursor.fetchall()
for tenant in tenants:
    print(f"Tenant: {tenant['display_name']} ({tenant['name']})")

# Get knowledge sources with chunks
cursor.execute("""
    SELECT
        ks.id,
        ks.name,
        ks.chunk_count,
        COUNT(kc.id) as active_chunks
    FROM knowledge_sources ks
    LEFT JOIN knowledge_chunks kc ON ks.id = kc.source_id AND kc.is_active = true
    GROUP BY ks.id
""")
sources = cursor.fetchall()
for source in sources:
    print(f"Source: {source['name']} - {source['active_chunks']} active chunks")

cursor.close()
conn.close()
```

## 2. 🔍 Qdrant Vector Database Access

### Connection Details
```
HTTP API: http://localhost:6333
gRPC API: http://localhost:6334
Collection: luran_knowledge
Dimensions: 768
Distance: Cosine
```

### Method A: Web Dashboard (Best Option)

**URL:** http://localhost:6333/dashboard

**Features:**
- Browse collections visually
- View vector data
- Search and filter
- Collection statistics
- Query testing

### Method B: REST API

**Get collection info:**
```bash
curl http://localhost:6333/collections/luran_knowledge | jq
```

**Response:**
```json
{
  "result": {
    "status": "green",
    "points_count": 0,
    "indexed_vectors_count": 0,
    "config": {
      "params": {
        "vectors": {
          "size": 768,
          "distance": "Cosine"
        }
      }
    }
  }
}
```

**List all collections:**
```bash
curl http://localhost:6333/collections | jq
```

**Search for vectors (example):**
```bash
curl -X POST http://localhost:6333/collections/luran_knowledge/points/search \
  -H "Content-Type: application/json" \
  -d '{
    "vector": [0.1, 0.2, ...],  # 768 dimensions
    "limit": 5,
    "with_payload": true
  }' | jq
```

**Get specific point:**
```bash
curl http://localhost:6333/collections/luran_knowledge/points/point-id | jq
```

**Scroll through points:**
```bash
curl -X POST http://localhost:6333/collections/luran_knowledge/points/scroll \
  -H "Content-Type: application/json" \
  -d '{
    "limit": 10,
    "with_payload": true,
    "with_vector": true
  }' | jq
```

### Method C: Python Script

```python
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue

# Connect
client = QdrantClient(url="http://localhost:6333")

# Get collection info
collection_info = client.get_collection("luran_knowledge")
print(f"Points count: {collection_info.points_count}")
print(f"Vector size: {collection_info.config.params.vectors.size}")

# Scroll through points
points = client.scroll(
    collection_name="luran_knowledge",
    limit=10,
    with_payload=True,
    with_vectors=True
)

for point in points[0]:
    print(f"\nPoint ID: {point.id}")
    print(f"Tenant ID: {point.payload.get('tenant_id')}")
    print(f"Chunk ID: {point.payload.get('chunk_id')}")
    print(f"Content preview: {point.payload.get('content', '')[:100]}...")

# Search by tenant
tenant_id = "bb24bf9e-63c2-4497-b5f5-e46dd916f7ea"
tenant_filter = Filter(
    must=[
        FieldCondition(
            key="tenant_id",
            match=MatchValue(value=tenant_id)
        )
    ]
)

# Count vectors for tenant
count = client.count(
    collection_name="luran_knowledge",
    count_filter=tenant_filter
)
print(f"\nVectors for tenant {tenant_id}: {count.count}")
```

### Method D: Using Your Application's Utility

```bash
# Check collection and count
poetry run python scripts/init_qdrant.py

# Test ingestion pipeline
poetry run python scripts/test_ingestion.py
```

## 3. 🔄 Common Queries

### Check Data Consistency

**PostgreSQL side:**
```sql
-- Count active chunks per source
SELECT
    ks.name as source_name,
    ks.chunk_count as reported_count,
    COUNT(kc.id) as actual_count
FROM knowledge_sources ks
LEFT JOIN knowledge_chunks kc ON ks.id = kc.source_id AND kc.is_active = true
GROUP BY ks.id, ks.name, ks.chunk_count;

-- Find chunks without vectors
SELECT kc.id, kc.vector_id, kc.content
FROM knowledge_chunks kc
WHERE kc.is_active = true;
```

**Qdrant side:**
```bash
# Count total vectors
curl http://localhost:6333/collections/luran_knowledge | jq '.result.points_count'

# Count by tenant (requires search with filter)
curl -X POST http://localhost:6333/collections/luran_knowledge/points/count \
  -H "Content-Type: application/json" \
  -d '{
    "filter": {
      "must": [
        {
          "key": "tenant_id",
          "match": {
            "value": "bb24bf9e-63c2-4497-b5f5-e46dd916f7ea"
          }
        }
      ]
    }
  }' | jq
```

### Verify Specific Upload

After uploading a document:

```sql
-- Get source details
SELECT * FROM knowledge_sources
WHERE name = 'your-document-name.txt';

-- Get its chunks
SELECT id, chunk_index, LEFT(content, 100) as content_preview, vector_id
FROM knowledge_chunks
WHERE source_id = 'source-uuid-from-above'
ORDER BY chunk_index;
```

Then check Qdrant:
```bash
# Check if vectors exist
curl -X POST http://localhost:6333/collections/luran_knowledge/points/scroll \
  -H "Content-Type: application/json" \
  -d '{
    "filter": {
      "must": [
        {
          "key": "source_id",
          "match": {
            "value": "source-uuid"
          }
        }
      ]
    },
    "limit": 100,
    "with_payload": true
  }' | jq '.result.points | length'
```

## 4. 🐛 Debugging

### Check if databases are accessible

```bash
# PostgreSQL
docker exec luran_db pg_isready -U user

# Qdrant
curl http://localhost:6333/healthz
```

### View logs

```bash
# API logs
docker logs luran_api --tail 100 -f

# Database logs
docker logs luran_db --tail 50

# Qdrant logs
docker logs luran_qdrant --tail 50
```

### Check connections

```bash
# PostgreSQL connections
docker exec luran_db psql -U user -d luran_db -c "SELECT COUNT(*) FROM pg_stat_activity;"

# Qdrant stats
curl http://localhost:6333/metrics
```

## 5. 📊 Monitoring Queries

### Database Size

```sql
-- Database size
SELECT pg_size_pretty(pg_database_size('luran_db'));

-- Table sizes
SELECT
    schemaname,
    tablename,
    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) AS size
FROM pg_tables
WHERE schemaname = 'public'
ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC;

-- Index sizes
SELECT
    schemaname,
    tablename,
    indexname,
    pg_size_pretty(pg_relation_size(indexrelid)) AS index_size
FROM pg_indexes
WHERE schemaname = 'public'
ORDER BY pg_relation_size(indexrelid) DESC;
```

### Activity Metrics

```sql
-- Recent conversations
SELECT
    DATE(created_at) as date,
    COUNT(*) as session_count
FROM conversation_sessions
GROUP BY DATE(created_at)
ORDER BY date DESC;

-- Messages per day
SELECT
    DATE(created_at) as date,
    COUNT(*) as message_count,
    AVG(confidence_score) as avg_confidence
FROM messages
WHERE role = 'ASSISTANT'
GROUP BY DATE(created_at)
ORDER BY date DESC;

-- Escalation rate
SELECT
    COUNT(*) FILTER (WHERE status = 'ESCALATED') * 100.0 / COUNT(*) as escalation_rate
FROM conversation_sessions;
```

## 6. 🔐 Security Notes

- **Production**: Change default passwords
- **Network**: Restrict database access to localhost or VPN
- **Backups**: Set up automated backups
- **SSL**: Enable SSL/TLS for PostgreSQL in production
- **Access Control**: Use Qdrant API keys in production
- **Monitoring**: Set up alerts for suspicious activity

## 7. 🛠️ Quick Maintenance

### Backup Database

```bash
# Backup
docker exec luran_db pg_dump -U user luran_db > backup_$(date +%Y%m%d).sql

# Restore
docker exec -i luran_db psql -U user -d luran_db < backup_20240115.sql
```

### Reset Qdrant Collection

```bash
# Delete collection
curl -X DELETE http://localhost:6333/collections/luran_knowledge

# Recreate
poetry run python scripts/init_qdrant.py
```

### Clear All Data (Development Only!)

```bash
# WARNING: This deletes everything!
docker-compose down -v
docker-compose up -d
poetry run alembic upgrade head
poetry run python scripts/seed_data.py
```

---

**Quick Access URLs:**
- API Docs: http://localhost:8000/api/v1/docs
- Qdrant Dashboard: http://localhost:6333/dashboard
- Database: localhost:5432 (use psql or GUI tool)
