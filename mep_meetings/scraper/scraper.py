# %%
from abc import ABC, abstractmethod
import httpx
from asyncio import Semaphore
import pandas as pd
from tqdm import tqdm
from tqdm.asyncio import tqdm_asyncio
from bs4 import BeautifulSoup
import urllib
from typing import List, Dict, Optional
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_fixed
import re


class BaseFetcher(ABC):  # Inherit from ABC (Abstract Base Class)
    def __init__(
        self, base_url: str = None, timeout: int = 10, max_connections: int = 3
    ) -> None:
        """
        Initialize the BaseFetcher with a base URL, timeout, and maximum connections.

        :param base_url: The base URL for fetching articles. Optional, the child class can circumvent this.
        :param timeout: The timeout for HTTP requests. Default is 10 seconds.
        :param max_connections: The maximum number of concurrent connections. Default is 3.
        """
        self.base_url = base_url
        self.article_links: List[str] = []
        self.articles: List[Dict[str, Optional[str]]] = []
        self.timeout = timeout
        self.semaphore = Semaphore(max_connections)

    @abstractmethod
    def construct_or_retrieve_links(self, page: int) -> None:
        """
        Retrieve article links from a given page.
        The links must be stored in the article_links attribute as a flat list of URLs.

        :param page: The page number to retrieve links from.
        """
        pass

    @abstractmethod
    def parse_article(
        self, article_html: str, article_url: str
    ) -> Optional[Dict[str, Optional[str]]]:
        """
        Parse a single article's HTML.

        :param article_html: The HTML content of the article.
        :param article_url: The URL of the article.
        :return: A dictionary containing parsed article data.
        """
        pass

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
    async def fetch_article(
        self, client: httpx.AsyncClient, article_url: str
    ) -> Optional[str]:
        """
        Fetch a single article with retry logic.

        :param client: The HTTP client to use for fetching.
        :param article_url: The URL of the article to fetch.
        :return: The HTML content of the article, or None if fetching failed.
        """
        try:
            response = await client.get(article_url, timeout=self.timeout)
            response.raise_for_status()
            return response.text
        except Exception as e:
            logger.warning(f"Failed to fetch {article_url}: {e}")
            logger.warning(f"Retrying {article_url}...")
            raise e  # Raise exception to trigger retry

    async def fetch_all_articles(self, pages: int) -> List[Optional[str]]:
        """
        Fetch all articles across multiple pages.

        :param pages: The number of pages to fetch articles from.
        :return: A list of HTML content for each article.
        """
        for page in tqdm(range(1, pages + 1), desc="Retrieving Links..."):
            try:
                self.construct_or_retrieve_links(page)
            except Exception as e:
                logger.warning(f"Failed to retrieve links from page {page}: {e}")
                raise e

        async with httpx.AsyncClient() as client:
            tasks = [
                self.fetch_article(client, article_url)
                for article_url in self.article_links
            ]
            wrapped_tasks = tqdm_asyncio.gather(*tasks, desc="Fetching Articles...")
            return await wrapped_tasks

    async def run_async(self, pages: int = 1) -> None:
        """
        Run the fetcher asynchronously to retrieve and parse articles.

        :param pages: The number of pages to fetch articles from.
        """
        responses = await self.fetch_all_articles(pages)
        self.articles = [
            self.parse_article(article_html, article_url)
            for article_html, article_url in tqdm(
                zip(responses, self.article_links),
                desc="Parsing Articles...",
                total=len(self.article_links),
            )
            if article_html is not None
        ]
        # self.articles

        # Filter out None values and flatten the list of lists of dictionaries into a single list of dictionaries
        self.articles = [
            item for sublist in self.articles if sublist is not None for item in sublist
        ]


class EuroparlMeetingFetcher(BaseFetcher):
    def __init__(
        self, referer_url: str, timeout: int = 10, max_connections: int = 8
    ) -> None:
        """
        Initialize the EuroparlMeetingFetcher with a specific base URL, timeout, and maximum connections.

        :param referer_url: The referer URL for fetching articles.
        :param timeout: The timeout for HTTP requests.
        :param max_connections: The maximum number of concurrent connections.

        Example usage:
        >>> fetcher = EuroparlMeetingFetcher(referer_url="https://www.europarl.europa.eu/meps/en/256864/ANDRAS+TIVADAR_KULJA/meetings/past")
        >>> asyncio.run(fetcher.run_async(pages=1))

        """
        super().__init__(
            base_url="https://www.europarl.europa.eu/meps/en/loadmore-meetings",  # Base URL for fetching articles
            timeout=timeout,
            max_connections=max_connections,
        )

        self.referer_url = referer_url
        self.article_links = [referer_url]
        self.member_id = self.extract_member_id(referer_url)

    @staticmethod
    def extract_member_id(referer_url) -> str:
        """
        Extract the member ID from the referer URL.
        Currently built-in to the scraper.
        Later on, this would be the entry point for the user to provide the member ID.

        :param referer_url: The referer URL containing the member ID.
        :return: The member ID extracted from the URL.

        Example usage:
        >>> fetcher = EuroparlMeetingFetcher()
        >>> fetcher.extract_member_id("https://www.europarl.europa.eu/meps/en/256864/ANDRAS+TIVADAR_KULJA/meetings/past")

        Example:
        https://www.europarl.europa.eu/meps/en/256864/ANDRAS+TIVADAR_KULJA/meetings/past
        The member ID is 256864.
        """
        match = re.search(r"/(\d+)/", referer_url)
        if match:
            return match.group(1)
        else:
            raise ValueError("Member ID not found in the referer URL")

    def construct_or_retrieve_links(self, page: int) -> None:
        """
        Here, we construct links to the HTML table responses.
        We utilize urllib.parse.urlencode to encode the parameters and append them to the base URL.
        We store them in a list in self.article_links.

        :param page: The page number to construct links for.
        """
        params = {
            "meetingType": "PAST",
            "memberId": self.member_id,
            "termId": "10",
            "page": str(page),
            "pageSize": "10",
        }
        constructed_url = f"{self.base_url}?{urllib.parse.urlencode(params)}"
        self.article_links.append(constructed_url)

    def parse_article(
        self, article_html: str, article_url: str
    ) -> Optional[Dict[str, Optional[str]]]:
        """
        Parse a page.

        :param article_html: The HTML content of the page.
        :param article_url: The URL of the page.
        :return: A dictionary containing parsed page data.
        """
        if len(article_html.strip()) == 0:
            return None
        try:
            page = int(re.search(r"page=(\d+)", article_url).group(1))
        except Exception:
            page = 0

        try:
            meetings = []
            soup = BeautifulSoup(article_html, "html.parser")
            headers = soup.select(".erpl_document-header")
            for header in headers:
                record = {}
                try:
                    record["Title"] = header.select_one(".t-item").get_text()
                except Exception as e:
                    logger.warning(f"Failed to retrieve title: {e}")
                    record["Title"] = None

                try:
                    record["Date"] = header.select_one("time").get("datetime")
                except Exception as e:
                    logger.warning(f"Failed to retrieve date: {e}")
                    record["Date"] = None

                try:
                    record["Place"] = header.select_one(
                        ".erpl_document-subtitle-location"
                    ).get_text()
                except Exception as e:
                    logger.warning(f"Failed to retrieve place: {e}")
                    record["Place"] = None

                try:
                    record["Capacity"] = (
                        header.select_one(".erpl_document-subtitle-capacity")
                        .get_text()
                        .strip()
                        .replace("\n", " - ")
                    )
                except Exception as e:
                    logger.warning(f"Failed to retrieve capacity: {e}")
                    record["Capacity"] = None

                try:
                    record["Code of associated committee or delegation"] = (
                        header.select_one(".erpl_badge-committee")
                        .get_text()
                        .strip()
                        .replace("\n", " - ")
                    )
                except Exception as e:
                    logger.warning(f"Failed to retrieve code: {e}")
                    record["Code of associated committee or delegation"] = None

                try:
                    record["Meeting with"] = (
                        header.select_one(".erpl_document-subtitle-author")
                        .get_text()
                        .strip()
                        .replace("\n", " - ")
                    )
                except Exception as e:
                    logger.warning(f"Failed to retrieve meeting: {e}")
                    record["Meeting with"] = None

                record["page_number"] = page
                meetings.append(record)

            return meetings
        except Exception as e:
            logger.error(f"Failed to parse page: {article_url}: {e}")
            return None


# %%
response = httpx.get(
    "https://www.europarl.europa.eu/meps/en/loadmore-meetings?meetingType=PAST&memberId=256864&termId=10&page=1000&pageSize=10"
)  # %%
response.status_code
# %%
response.content
# %%
