"""A single company careers page — §2.1, "build last".

Lowest yield per unit of effort, highest coverage: every company has one, and
almost none of them agree on anything. Two passes, cheapest first:

1. **schema.org `JobPosting` JSON-LD**, same as the aggregator. Free, exact, and
   surprisingly common because Google for Jobs requires it.
2. **the LLM layer**, only when the page has no structured data and
   `jobs.career_page_llm` is on. Extraction is a classification task with all
   the evidence in the prompt, which is what the existing scoring stack is for.

Off by default (`jobs.sources_enabled` ships without `career_page`): it is the
adapter that fetches arbitrary HTML, so it is opt-in.
"""

from __future__ import annotations

import logging
from typing import Any

from ...config import Settings, get_settings
from .aggregator import parse_job_postings
from .base import JobPosting, PoliteClient, strip_html

log = logging.getLogger(__name__)


class CareerPageSource:
    kind = "career_page"

    def __init__(
        self,
        name: str,
        url: str,
        *,
        company_id: int | None = None,
        company_name: str | None = None,
        settings: Settings | None = None,
        client: PoliteClient | None = None,
        llm_extractor: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.name = name
        self.url = url
        self.company_id = company_id
        self.company_name = company_name
        self.client = client or PoliteClient(self.settings)
        #: Injected rather than imported, so this module has no LLM dependency
        #: and the tests never need one. Signature: `(text, url) -> [JobPosting]`.
        self.llm_extractor = llm_extractor

    def fetch(self, *, no_cache: bool = False) -> list[JobPosting]:
        html = self.client.get(self.url, no_cache=no_cache, accept="text/html")
        postings = parse_job_postings(html, self.url)
        if postings:
            log.info("%s: %d posting(s) from JSON-LD", self.name, len(postings))
            return postings
        if self.llm_extractor is None:
            log.info("%s: no JobPosting structured data, and no LLM extractor", self.name)
            return []
        text = strip_html(html, limit=20000) or ""
        postings = list(self.llm_extractor(text, self.url) or [])
        log.info("%s: %d posting(s) from the LLM extractor", self.name, len(postings))
        return postings
