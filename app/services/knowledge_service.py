"""Knowledge service for managing documents and knowledge chunks."""

import hashlib
import uuid as _uuid
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from loguru import logger
from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.knowledge import (
    ChunkVisibility,
    KnowledgeChunk,
    KnowledgeSource,
    SourceStatus,
    SourceType,
)
from app.schemas.knowledge import (
    KnowledgeChunkCreate,
    KnowledgeChunkResponse,
    KnowledgeSourceCreate,
    KnowledgeSourceDetailResponse,
    KnowledgeSourceResponse,
    KnowledgeSourceUpdate,
)
from app.utils.knowledge_failures import FailureCode, KnowledgeError, check_document_text


class KnowledgeService:
    """Service for knowledge management and document processing."""

    def __init__(self, db: Session):
        """Initialize knowledge service with database session."""
        self.db = db

    def _compute_content_hash(self, content: str) -> str:
        """
        Compute SHA256 hash of content.

        Args:
            content: Content to hash

        Returns:
            Hex digest of SHA256 hash
        """
        return hashlib.sha256(content.encode()).hexdigest()

    def create_source(self, source_data: KnowledgeSourceCreate) -> KnowledgeSourceResponse:
        """
        Create a new knowledge source with version management.

        Implements retry logic to handle race conditions where multiple processes
        attempt to create the same version simultaneously.

        Args:
            source_data: Source creation data

        Returns:
            Created source response

        Raises:
            ValueError: If failed to create source after max retries
        """
        # Retry loop for handling race conditions
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Step 1: Query for existing duplicate with row lock
                stmt = (
                    select(KnowledgeSource)
                    .where(
                        and_(
                            KnowledgeSource.tenant_id == source_data.tenant_id,
                            KnowledgeSource.content_hash == source_data.content_hash,
                            KnowledgeSource.status != SourceStatus.INACTIVE,
                        )
                    )
                    .with_for_update()
                )

                existing = self.db.execute(stmt).scalar_one_or_none()

                # Step 2: Decide version
                if existing:
                    version = existing.version + 1
                else:
                    version = 1

                # Step 3: Create and commit
                db_source = KnowledgeSource(
                    tenant_id=source_data.tenant_id,
                    name=source_data.name,
                    source_type=source_data.source_type,
                    file_url=source_data.file_url,
                    content_hash=source_data.content_hash,
                    version=version,
                    status=SourceStatus.PROCESSING,
                    chunk_count=0,
                    meta=source_data.meta or {},
                    uploaded_by=source_data.uploaded_by,
                    source_category=source_data.source_category,
                    source_url=source_data.source_url,
                    file_size_bytes=source_data.file_size_bytes,
                )
                self.db.add(db_source)
                self.db.commit()
                self.db.refresh(db_source)

                logger.info(
                    f"Created knowledge source: {db_source.name} "
                    f"(version {version}, tenant {source_data.tenant_id})"
                )
                return KnowledgeSourceResponse.model_validate(db_source)

            except IntegrityError as e:
                # Another transaction inserted the same version
                self.db.rollback()
                if attempt < max_retries - 1:
                    logger.warning(f"Version conflict detected, retrying (attempt {attempt + 1})")
                    continue
                else:
                    raise ValueError(
                        f"Failed to create source after {max_retries} attempts due to version conflicts"
                    ) from e
        raise ValueError("Failed to create source after max retries")

    def get_source(
        self, source_id: UUID, include_chunks: bool = False
    ) -> KnowledgeSourceResponse | None:
        """
        Get knowledge source by ID.

        Args:
            source_id: Source UUID
            include_chunks: Whether to include all chunks

        Returns:
            Source response if found, None otherwise
        """
        source_stmt = select(KnowledgeSource).where(KnowledgeSource.id == source_id)
        source = self.db.execute(source_stmt).scalar_one_or_none()

        if not source:
            return None

        if include_chunks:
            return KnowledgeSourceDetailResponse.model_validate(source)
        else:
            return KnowledgeSourceResponse.model_validate(source)

    def update_source(
        self, source_id: UUID, source_data: KnowledgeSourceUpdate
    ) -> KnowledgeSourceResponse:
        """
        Update knowledge source.

        Args:
            source_id: Source UUID
            source_data: Updated source data

        Returns:
            Updated source response

        Raises:
            ValueError: If source not found
        """
        source_stmt = select(KnowledgeSource).where(KnowledgeSource.id == source_id)
        source = self.db.execute(source_stmt).scalar_one_or_none()

        if not source:
            raise ValueError("Knowledge source not found")

        # Update fields
        update_data = source_data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(source, field, value)

        self.db.commit()
        self.db.refresh(source)

        return KnowledgeSourceResponse.model_validate(source)

    def mark_source_active(self, source_id: UUID) -> KnowledgeSourceResponse:
        """
        Mark a source as active after successful processing.

        Args:
            source_id: Source UUID

        Returns:
            Updated source response

        Raises:
            ValueError: If source not found
        """
        # Recompute chunk count to ensure accuracy before activation
        self.recompute_chunk_count(source_id)

        source_stmt = select(KnowledgeSource).where(KnowledgeSource.id == source_id)
        source = self.db.execute(source_stmt).scalar_one_or_none()

        if not source:
            raise ValueError("Knowledge source not found")

        source.status = SourceStatus.ACTIVE
        source.failure_code = None

        self.db.commit()
        self.db.refresh(source)

        return KnowledgeSourceResponse.model_validate(source)

    def mark_source_failed(
        self, source_id: UUID, failure_code: FailureCode | None = None
    ) -> KnowledgeSourceResponse:
        """
        Mark a source as failed during processing.

        FAILED sources never count toward the plan's source cap.

        Args:
            source_id: Source UUID
            failure_code: Why it failed (shown to the tenant as `failure_reason`)

        Returns:
            Updated source response

        Raises:
            ValueError: If source not found
        """
        stmt = select(KnowledgeSource).where(KnowledgeSource.id == source_id)
        source = self.db.execute(stmt).scalar_one_or_none()

        if not source:
            raise ValueError("Knowledge source not found")

        source.status = SourceStatus.FAILED
        source.failure_code = failure_code.value if failure_code else None

        self.db.commit()
        self.db.refresh(source)

        return KnowledgeSourceResponse.model_validate(source)

    def fail_source_and_cleanup(self, source_id: UUID, failure_code: FailureCode | None) -> None:
        """
        Mark a source FAILED and remove anything half-written for it, so a failed upload leaves
        no searchable fragments. Best effort: never raises (callers are already handling an error).
        """
        for attempt in (1, 2):
            try:
                self.mark_source_failed(source_id, failure_code)
                for chunk in self.db.execute(
                    select(KnowledgeChunk).where(KnowledgeChunk.source_id == source_id)
                ).scalars():
                    chunk.is_active = False
                self.db.commit()
                break
            except Exception as e:  # noqa: BLE001
                # The session may be in a failed transaction (the error being handled):
                # clear it once and retry before giving up.
                self.db.rollback()
                if attempt == 2:
                    logger.error(f"Could not mark source {source_id} failed: {e}")
                    return
        try:
            from app.utils.qdrant_client import qdrant_manager

            qdrant_manager.delete_by_source_id(source_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not remove vectors of failed source {source_id}: {e}")

    def deactivate_old_versions(
        self, tenant_id: UUID, content_hash: str, current_version: int
    ) -> None:
        """
        Deactivate old versions of a knowledge source.

        Args:
            tenant_id: Tenant UUID
            content_hash: Content hash
            current_version: Current version number
        """
        # Deactivate old source versions
        stmt = select(KnowledgeSource).where(
            and_(
                KnowledgeSource.tenant_id == tenant_id,
                KnowledgeSource.content_hash == content_hash,
                KnowledgeSource.version < current_version,
                KnowledgeSource.status == SourceStatus.ACTIVE,
            )
        )
        old_sources = self.db.execute(stmt).scalars().all()

        for source in old_sources:
            source.status = SourceStatus.INACTIVE

        # Deactivate associated chunks
        for source in old_sources:
            stmt_chunks = select(KnowledgeChunk).where(
                and_(KnowledgeChunk.source_id == source.id, KnowledgeChunk.is_active.is_(True))
            )
            chunks = self.db.execute(stmt_chunks).scalars().all()

            for chunk in chunks:
                chunk.is_active = False

        self.db.commit()

    def create_chunk(self, chunk_data: KnowledgeChunkCreate) -> KnowledgeChunkResponse:
        """
        Create a knowledge chunk.

        Args:
            chunk_data: Chunk creation data

        Returns:
            Created chunk response
        """
        db_chunk = KnowledgeChunk(
            tenant_id=chunk_data.tenant_id,
            source_id=chunk_data.source_id,
            content=chunk_data.content,
            chunk_index=chunk_data.chunk_index,
            vector_id=chunk_data.vector_id,
            meta=chunk_data.meta,
            visibility=chunk_data.visibility,
            language=chunk_data.language,
            version=chunk_data.version,
            is_active=True,
        )

        self.db.add(db_chunk)
        self.db.commit()
        self.db.refresh(db_chunk)

        return KnowledgeChunkResponse.model_validate(db_chunk)

    def recompute_chunk_count(self, source_id: UUID) -> int:
        """
        Recompute chunk count from actual chunks (source of truth).

        This eliminates drift from incremental updates and concurrent operations.

        Args:
            source_id: Knowledge source ID

        Returns:
            Actual chunk count
        """
        from sqlalchemy import func

        # Count active chunks for this source
        count_stmt = select(func.count()).where(
            and_(KnowledgeChunk.source_id == source_id, KnowledgeChunk.is_active.is_(True))
        )
        actual_count = self.db.execute(count_stmt).scalar() or 0

        # Update source
        stmt = select(KnowledgeSource).where(KnowledgeSource.id == source_id)
        source = self.db.execute(stmt).scalar_one_or_none()

        if source:
            source.chunk_count = actual_count
            self.db.commit()
            logger.debug(f"Recomputed chunk_count for source {source_id}: {actual_count}")

        return actual_count

    def get_chunk(self, chunk_id: UUID) -> KnowledgeChunkResponse | None:
        """
        Get knowledge chunk by ID.

        Args:
            chunk_id: Chunk UUID

        Returns:
            Chunk response if found, None otherwise
        """
        stmt = select(KnowledgeChunk).where(KnowledgeChunk.id == chunk_id)
        chunk = self.db.execute(stmt).scalar_one_or_none()

        if not chunk:
            return None

        return KnowledgeChunkResponse.model_validate(chunk)

    def get_chunks_batch(
        self, chunk_ids: list[UUID], tenant_id: UUID | None = None
    ) -> dict[UUID, KnowledgeChunkResponse]:
        """
        Retrieve multiple chunks in a single query (fixes N+1 pattern).

        Args:
            chunk_ids: List of chunk UUIDs to fetch
            tenant_id: Optional tenant filter for security

        Returns:
            Dict mapping chunk_id to KnowledgeChunkResponse (PUBLIC chunks only)
        """
        if not chunk_ids:
            return {}

        stmt = (
            select(KnowledgeChunk)
            .where(KnowledgeChunk.id.in_(chunk_ids))
            .where(KnowledgeChunk.is_active.is_(True))
            .where(
                KnowledgeChunk.visibility == ChunkVisibility.PUBLIC
            )  # Security: prevent internal/restricted doc leaks
        )

        if tenant_id:
            stmt = stmt.where(KnowledgeChunk.tenant_id == tenant_id)

        chunks = self.db.execute(stmt).scalars().all()

        # Return as dict for fast lookup while preserving order
        return {chunk.id: KnowledgeChunkResponse.model_validate(chunk) for chunk in chunks}

    def get_source_chunks(
        self, source_id: UUID, active_only: bool = True
    ) -> list[KnowledgeChunkResponse]:
        """
        Get all chunks for a source.

        Args:
            source_id: Source UUID
            active_only: Whether to return only active chunks

        Returns:
            List of chunk responses
        """
        stmt = select(KnowledgeChunk).where(KnowledgeChunk.source_id == source_id)

        if active_only:
            stmt = stmt.where(KnowledgeChunk.is_active.is_(True))

        stmt = stmt.order_by(KnowledgeChunk.chunk_index)

        chunks = self.db.execute(stmt).scalars().all()

        return [KnowledgeChunkResponse.model_validate(c) for c in chunks]

    def get_tenant_sources(
        self, tenant_id: UUID, status: SourceStatus | None = None
    ) -> list[KnowledgeSourceResponse]:
        """
        Get all knowledge sources for a tenant.

        Args:
            tenant_id: Tenant UUID
            status: Optional status filter

        Returns:
            List of source responses
        """
        stmt = select(KnowledgeSource).where(KnowledgeSource.tenant_id == tenant_id)

        if status:
            stmt = stmt.where(KnowledgeSource.status == status)
        else:
            # Default listing hides tombstones: INACTIVE = soft-deleted or a
            # superseded old version, neither of which the user can act on.
            stmt = stmt.where(KnowledgeSource.status != SourceStatus.INACTIVE)

        stmt = stmt.order_by(KnowledgeSource.created_at.desc())

        sources = self.db.execute(stmt).scalars().all()

        return [KnowledgeSourceResponse.model_validate(s) for s in sources]

    def check_knowledge_source_limit(
        self,
        tenant_id: UUID,
        source_category: str = "file",
    ) -> None:
        """
        Check if tenant can add another knowledge source.

        Args:
            tenant_id: Tenant UUID
            source_category: "file" or "url"

        Raises:
            HTTPException 403: Limit reached
        """
        from app.services.tenant_service import TenantService

        tenant_service = TenantService(self.db)
        capabilities = tenant_service.get_capabilities(tenant_id)

        if not capabilities:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Tenant capabilities not found"
            )

        # Check if uploads enabled
        if not capabilities.can_upload_knowledge:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Knowledge uploads disabled for your subscription",
            )

        # Check URL source permission
        if source_category == "url" and not capabilities.can_use_url_sources:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "url_sources_disabled",
                    "message": "URL sources not enabled for trial tier. Upgrade to Lite for webpage scraping.",
                    "upgrade_url": "/dashboard/upgrade",
                },
            )

        # Count existing sources (files + URLs combined). Count PROCESSING as well
        # as ACTIVE: an in-flight import/upload will become ACTIVE, so excluding it
        # here would let a tenant overshoot the cap while ingestion is pending
        # (e.g. import audit findings, then upload files before the imports finish).
        # Matches AuditService._existing_source_count for the shared cap.
        total_sources = (
            self.db.query(KnowledgeSource)
            .filter(
                KnowledgeSource.tenant_id == tenant_id,
                KnowledgeSource.status.in_([SourceStatus.ACTIVE, SourceStatus.PROCESSING]),
            )
            .count()
        )

        # Check combined limit
        if capabilities.max_knowledge_sources is not None:
            if total_sources >= capabilities.max_knowledge_sources:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "error": "knowledge_limit_reached",
                        "message": f"Knowledge source limit reached ({total_sources}/{capabilities.max_knowledge_sources}).",
                        "current_count": total_sources,
                        "limit": capabilities.max_knowledge_sources,
                        "upgrade_message": "Upgrade to Professional for 30 sources or Enterprise for unlimited.",
                    },
                )

    def calculate_tenant_storage_mb(self, tenant_id: UUID) -> float:
        """
        Calculate total storage used by tenant in MB.

        Includes ACTIVE and PROCESSING sources to prevent exceeding limits
        while files are being processed.

        Args:
            tenant_id: Tenant UUID

        Returns:
            Storage in MB
        """
        total_bytes = (
            self.db.query(func.sum(KnowledgeSource.file_size_bytes))
            .filter(
                KnowledgeSource.tenant_id == tenant_id,
                KnowledgeSource.status.in_([SourceStatus.ACTIVE, SourceStatus.PROCESSING]),
            )
            .scalar()
            or 0
        )

        return total_bytes / 1024 / 1024  # Convert to MB

    def get_active_chunks_for_tenant(self, tenant_id: UUID) -> list[KnowledgeChunkResponse]:
        """
        Get all active chunks for a tenant (for RAG retrieval).

        Args:
            tenant_id: Tenant UUID

        Returns:
            List of active chunk responses
        """
        stmt = select(KnowledgeChunk).where(
            and_(KnowledgeChunk.tenant_id == tenant_id, KnowledgeChunk.is_active.is_(True))
        )

        chunks = self.db.execute(stmt).scalars().all()

        return [KnowledgeChunkResponse.model_validate(c) for c in chunks]

    def delete_source(self, source_id: UUID) -> bool:
        """
        Delete a knowledge source and its chunks (soft delete).

        Args:
            source_id: Source UUID

        Returns:
            True if deleted successfully

        Raises:
            ValueError: If source not found
        """
        stmt = select(KnowledgeSource).where(KnowledgeSource.id == source_id)
        source = self.db.execute(stmt).scalar_one_or_none()

        if not source:
            raise ValueError("Knowledge source not found")

        # Soft delete: mark as inactive
        source.status = SourceStatus.INACTIVE

        # Deactivate associated chunks
        stmt_chunks = select(KnowledgeChunk).where(KnowledgeChunk.source_id == source_id)
        chunks = self.db.execute(stmt_chunks).scalars().all()

        for chunk in chunks:
            chunk.is_active = False

        self.db.commit()

        # Delete vectors from Qdrant
        try:
            from app.utils.qdrant_client import qdrant_manager

            qdrant_manager.delete_by_source_id(source_id)
        except Exception as e:
            logger.warning(f"Failed to delete vectors from Qdrant: {e}")

        return True

    async def create_source_and_chunks(
        self,
        tenant_id: UUID,
        document_text: str,
        document_name: str,
        source_type: SourceType = SourceType.HELP_DOC,
        document_metadata: dict[str, Any] | None = None,
        uploaded_by: UUID | None = None,
        source_category: str = "file",
        source_url: str | None = None,
        file_size_bytes: int | None = None,
    ) -> KnowledgeSourceResponse:
        """
        Create source and chunks WITHOUT embeddings.

        This is used by the background worker to prepare chunks before
        rate-limited embedding generation.

        Steps:
        1. Compute content hash
        2. Create knowledge source (status: PROCESSING)
        3. Chunk document text
        4. Create chunk records in database (without vector_id)

        Args:
            tenant_id: Tenant UUID
            document_text: Full document text
            document_name: Document name/filename
            source_type: Type of knowledge source
            document_metadata: Optional metadata
            uploaded_by: Optional user ID

        Returns:
            Created knowledge source

        Raises:
            ValueError: If chunking fails
        """
        from app.utils.chunking import text_chunker

        logger.info(f"Creating source and chunks (without embeddings) for '{document_name}'")

        problem = check_document_text(document_text)
        if problem:
            raise KnowledgeError(problem)  # nothing created yet: no source row to clean up

        source = None  # Initialize to avoid UnboundLocalError in except block

        try:
            # Step 1: Compute content hash
            content_hash = self._compute_content_hash(document_text)

            # Step 2: Create knowledge source
            source_data = KnowledgeSourceCreate(
                tenant_id=tenant_id,
                name=document_name,
                source_type=source_type,
                file_url=None,
                content_hash=content_hash,
                meta=document_metadata or {},
                uploaded_by=uploaded_by,
                source_category=source_category,
                source_url=source_url,
                file_size_bytes=file_size_bytes,
            )
            source = self.create_source(source_data)
            logger.info(f"Created knowledge source: {source.id} (version {source.version})")

            # Step 3: Chunk the document text
            logger.info("Chunking document text...")
            chunks_data = text_chunker.chunk_text(
                text=document_text,
                metadata={
                    "source_name": document_name,
                    "source_type": source_type.value,
                    **(document_metadata or {}),
                },
            )
            logger.info(f"Created {len(chunks_data)} chunks")

            if not chunks_data:
                logger.warning("No chunks created from document")
                raise KnowledgeError(FailureCode.NO_TEXT_EXTRACTED)

            # Step 4: Create chunk records (without embeddings)
            for i, chunk_data in enumerate(chunks_data):
                chunk_create = KnowledgeChunkCreate(
                    tenant_id=tenant_id,
                    source_id=source.id,
                    content=chunk_data["content"],
                    chunk_index=i,
                    # Placeholder (also a valid Qdrant id); the worker sets the real one.
                    vector_id=str(_uuid.uuid4()),
                    meta=chunk_data.get("metadata", {}),
                    visibility=ChunkVisibility.PUBLIC,
                    language="en",
                    version=source.version,
                )
                self.create_chunk(chunk_create)

            logger.success(f"Created {len(chunks_data)} chunk records (awaiting embedding)")

            return source

        except KnowledgeError as e:
            logger.error(f"Error creating source and chunks: {e}")
            if source:
                self.mark_source_failed(source.id, e.failure)
            raise
        except Exception as e:
            logger.error(f"Error creating source and chunks: {e}")
            if source:
                self.mark_source_failed(source.id)
            raise

    async def fill_existing_source_with_chunks(
        self,
        source_id: UUID,
        document_text: str,
        document_name: str,
        source_type: SourceType,
        tenant_id: UUID,
        document_metadata: dict[str, Any] | None = None,
    ) -> KnowledgeSourceResponse:
        """Chunk `document_text` and attach chunks to an already-created KnowledgeSource.

        Used by the auditor import path where `import_entries` pre-creates the
        KnowledgeSource (status=PROCESSING) and records its ID on the AuditEntry.
        The worker calls this instead of `create_source_and_chunks` so that exactly
        ONE KnowledgeSource exists per import — no orphan, no permanently-PROCESSING
        duplicate.

        The source must already exist and be owned by `tenant_id`.  Its status
        remains PROCESSING after this call; the caller (process_document_ingestion)
        transitions it to ACTIVE/FAILED after embedding.
        """
        from app.utils.chunking import text_chunker

        stmt = select(KnowledgeSource).where(KnowledgeSource.id == source_id)
        db_source = self.db.execute(stmt).scalar_one_or_none()
        if not db_source:
            raise ValueError(f"KnowledgeSource {source_id} not found")
        if db_source.tenant_id != tenant_id:
            raise ValueError(f"KnowledgeSource {source_id} does not belong to tenant {tenant_id}")

        logger.info(f"Filling existing source {source_id} with chunks for '{document_name}'")

        chunks_data = text_chunker.chunk_text(
            text=document_text,
            metadata={
                "source_name": document_name,
                "source_type": source_type.value,
                **(document_metadata or {}),
            },
        )

        if not chunks_data:
            logger.warning("No chunks created from document")
            self.mark_source_failed(source_id, FailureCode.NO_TEXT_EXTRACTED)
            raise KnowledgeError(FailureCode.NO_TEXT_EXTRACTED)

        for i, chunk_data in enumerate(chunks_data):
            # Provisional vector_id satisfies the NOT NULL DB constraint; the
            # worker overwrites it with the real Qdrant point ID after embedding.
            provisional_vector_id = str(_uuid.uuid4())

            chunk_create = KnowledgeChunkCreate(
                tenant_id=tenant_id,
                source_id=source_id,
                content=chunk_data["content"],
                chunk_index=i,
                vector_id=provisional_vector_id,
                meta=chunk_data.get("metadata", {}),
                visibility=ChunkVisibility.PUBLIC,
                language="en",
                version=db_source.version,
            )
            self.create_chunk(chunk_create)

        logger.success(f"Created {len(chunks_data)} chunk records for existing source {source_id}")
        return KnowledgeSourceResponse.model_validate(db_source)

    async def process_document(
        self,
        tenant_id: UUID,
        document_text: str,
        document_name: str,
        source_type: SourceType = SourceType.HELP_DOC,
        document_metadata: dict[str, Any] | None = None,
        uploaded_by: UUID | None = None,
        source_category: str = "file",
        source_url: str | None = None,
        file_size_bytes: int | None = None,
    ) -> KnowledgeSourceResponse:
        """
        Process a document: chunk, embed, and store in PostgreSQL + Qdrant.

        This is the main ingestion pipeline that orchestrates:
        1. Content hashing and versioning
        2. Source creation
        3. Text chunking
        4. Embedding generation
        5. Dual storage (PostgreSQL + Qdrant)

        Args:
            tenant_id: Tenant UUID
            document_text: Full document text
            document_name: Document name/filename
            source_type: Type of knowledge source
            document_metadata: Optional metadata for the source
            uploaded_by: Optional user ID who uploaded

        Returns:
            Created knowledge source response

        Raises:
            Exception: If processing fails
        """
        from app.services.knowledge_indexing import embed_and_store_chunk
        from app.utils.chunking import text_chunker
        from app.utils.qdrant_client import qdrant_manager

        logger.info(f"Starting document processing for '{document_name}' (tenant: {tenant_id})")

        # Reject before any source exists, so a too-short paste leaves nothing behind.
        problem = check_document_text(document_text)
        if problem:
            raise KnowledgeError(problem)

        source = None
        try:
            # Step 1: Compute content hash for versioning
            content_hash = self._compute_content_hash(document_text)

            # Step 2: Create knowledge source
            source_data = KnowledgeSourceCreate(
                tenant_id=tenant_id,
                name=document_name,
                source_type=source_type,
                file_url=None,
                content_hash=content_hash,
                meta=document_metadata or {},
                uploaded_by=uploaded_by,
                source_category=source_category,
                source_url=source_url,
                file_size_bytes=file_size_bytes,
            )
            source = self.create_source(source_data)
            logger.info(f"Created knowledge source: {source.id} (version {source.version})")

            # Step 3: Chunk the document text
            chunks_data = text_chunker.chunk_text(
                text=document_text,
                metadata={
                    "source_name": document_name,
                    "source_type": source_type.value,
                    **(document_metadata or {}),
                },
            )
            logger.info(f"Created {len(chunks_data)} chunks")
            if not chunks_data:
                raise KnowledgeError(FailureCode.NO_TEXT_EXTRACTED)

            # Step 4: Make sure the collection exists (idempotent), then embed + index each chunk
            if not qdrant_manager.ensure_collection():
                raise KnowledgeError(FailureCode.VECTOR_STORE_UNAVAILABLE)

            for i, chunk_data in enumerate(chunks_data):
                # The row's vector_id IS the Qdrant point id, so it must be a UUID.
                vector_id = str(_uuid.uuid4())
                created_chunk = self.create_chunk(
                    KnowledgeChunkCreate(
                        tenant_id=tenant_id,
                        source_id=source.id,
                        content=chunk_data["content"],
                        chunk_index=i,
                        vector_id=vector_id,
                        meta={
                            "token_count": chunk_data.get("token_count"),
                            **chunk_data.get("metadata", {}),
                        },
                        visibility=ChunkVisibility.PUBLIC,
                        language="en",
                        version=source.version,
                    )
                )
                failure = await embed_and_store_chunk(
                    tenant_id=tenant_id,
                    source_id=source.id,
                    source_name=document_name,
                    source_type=source_type.value,
                    version=source.version,
                    chunk_id=created_chunk.id,
                    chunk_index=i,
                    content=chunk_data["content"],
                    vector_id=vector_id,
                )
                if failure:
                    # All-or-nothing: a document with missing pieces would answer wrongly.
                    raise KnowledgeError(failure)

            logger.info(f"Indexed {len(chunks_data)} chunks")

            # Step 5: Mark source as active
            self.mark_source_active(source.id)
            logger.info(f"Document processing complete for '{document_name}'")

            # Step 6: Deactivate old versions if this is a new version
            if source.version > 1:
                logger.info(f"Deactivating old versions (version < {source.version})")
                self.deactivate_old_versions(tenant_id, content_hash, source.version)

            # Refresh source to get updated chunk count
            refreshed_source = self.get_source(source.id)
            if refreshed_source is None:
                raise ValueError("Failed to refresh source after processing")
            return refreshed_source

        except KnowledgeError as e:
            logger.error(f"Document '{document_name}' failed: {e.code}")
            if source:
                self.fail_source_and_cleanup(source.id, e.failure)
            raise
        except Exception as e:
            logger.error(f"Error processing document '{document_name}': {e}")
            if source:
                self.fail_source_and_cleanup(source.id, None)
            raise

    async def reprocess_source(self, source_id: UUID) -> KnowledgeSourceResponse:
        """
        Reprocess an existing source (re-chunk and re-embed).

        Useful for updating embeddings or chunk strategy without uploading new document.

        Args:
            source_id: Source UUID to reprocess

        Returns:
            Updated knowledge source

        Raises:
            ValueError: If source not found or has no original content
        """
        # Note: This would require storing original document content
        # For now, this is a placeholder for future implementation
        raise NotImplementedError("Reprocessing requires storing original document content")
