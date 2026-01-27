"""Service for scraping webpage content."""

from typing import Optional
import httpx
from bs4 import BeautifulSoup
from loguru import logger
from fastapi import HTTPException, status


class URLScraperService:
    """Service for scraping webpage content."""

    TIMEOUT = 30  # seconds
    MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10 MB

    async def scrape_url(self, url: str) -> dict:
        """
        Scrape webpage and extract text content.

        Args:
            url: Webpage URL to scrape

        Returns:
            dict with title, content, url, content_length

        Raises:
            HTTPException: On scraping failure
        """
        try:
            async with httpx.AsyncClient(timeout=self.TIMEOUT) as client:
                response = await client.get(url, follow_redirects=True)
                response.raise_for_status()

                # Check content length
                content_length = int(response.headers.get('content-length', 0))
                if content_length > self.MAX_CONTENT_LENGTH:
                    raise ValueError(f"Content too large: {content_length} bytes")

                # Parse HTML
                soup = BeautifulSoup(response.text, 'html.parser')

                # Extract title
                title = soup.find('title')
                title_text = title.get_text(strip=True) if title else url

                # Remove script and style elements
                for element in soup(['script', 'style', 'nav', 'footer', 'header']):
                    element.decompose()

                # Get text content
                text = soup.get_text(separator='\n', strip=True)

                # Clean up whitespace
                lines = (line.strip() for line in text.splitlines())
                text = '\n'.join(line for line in lines if line)

                if len(text) < 100:
                    raise ValueError("Insufficient content extracted")

                return {
                    "title": title_text,
                    "content": text,
                    "url": str(response.url),  # Final URL after redirects
                    "content_length": len(text),
                }

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error scraping {url}: {e}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to fetch URL: {e.response.status_code}"
            )
        except Exception as e:
            logger.error(f"Error scraping {url}: {e}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to scrape URL: {str(e)}"
            )
