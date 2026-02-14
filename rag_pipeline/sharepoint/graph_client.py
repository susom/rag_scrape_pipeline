"""
Microsoft Graph API SharePoint Client for RAG Pipeline.

Provides methods to:
- Pull site pages
- Pull pages content
- Pull defined lists
- Access drive root

Handles pagination and authentication via Azure AD.
"""

import os
import time
import requests
from typing import Optional, Generator, Any
from rag_pipeline.utils.logger import setup_logger

logger = setup_logger()

# Google Cloud Secret Manager integration
try:
    from google.cloud import secretmanager
    HAS_SECRET_MANAGER = True
except ImportError:
    HAS_SECRET_MANAGER = False
    logger.warning("google-cloud-secret-manager not installed. Using environment variables for credentials.")


class SharePointGraphClient:
    """
    Microsoft Graph API client for SharePoint operations.

    Authenticates using Azure AD app credentials and provides methods
    to interact with SharePoint sites, pages, lists, and drives.
    """

    GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"
    GRAPH_API_BETA = "https://graph.microsoft.com/beta"
    TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

    # Default page size for pagination
    DEFAULT_PAGE_SIZE = 100

    # Rate limiting settings
    MAX_RETRIES = 3
    RETRY_DELAY = 1  # seconds

    def __init__(
        self,
        site_hostname: str,
        site_path: str = "",
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        tenant_id: Optional[str] = None,
        gcp_project_id: Optional[str] = None,
    ):
        """
        Initialize the SharePoint Graph client.

        Args:
            site_hostname: SharePoint site hostname (e.g., 'contoso.sharepoint.com')
            site_path: Site path (e.g., '/sites/MySite')
            client_id: Azure AD app client ID (optional, will fetch from secrets)
            client_secret: Azure AD app client secret (optional, will fetch from secrets)
            tenant_id: Azure AD tenant ID (optional, will fetch from secrets)
            gcp_project_id: GCP project ID for Secret Manager (optional)
        """
        self.site_hostname = site_hostname
        self.site_path = site_path.rstrip('/') if site_path else ""
        self.gcp_project_id = gcp_project_id or os.getenv("GCP_PROJECT_ID", "")

        # Load credentials
        self.client_id = client_id or self._get_secret("SHAREPOINT_CLIENT_ID")
        self.client_secret = client_secret or self._get_secret("SHAREPOINT_CLIENT_SECRET")
        self.tenant_id = tenant_id or self._get_secret("SHAREPOINT_TENANT_ID")

        # Token cache
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0

        # Site ID cache
        self._site_id: Optional[str] = None

        logger.info(f"SharePoint client initialized for {site_hostname}{site_path}")

    def _get_secret(self, secret_name: str) -> str:
        """
        Get secret from Google Cloud Secret Manager or environment variable.

        Args:
            secret_name: Name of the secret

        Returns:
            Secret value
        """
        # First try environment variable
        env_value = os.getenv(secret_name, "").strip()
        if env_value:
            logger.debug(f"Loaded {secret_name} from environment variable")
            return env_value

        # Try Google Cloud Secret Manager
        if HAS_SECRET_MANAGER and self.gcp_project_id:
            try:
                client = secretmanager.SecretManagerServiceClient()
                name = f"projects/{self.gcp_project_id}/secrets/{secret_name}/versions/latest"
                response = client.access_secret_version(request={"name": name})
                logger.debug(f"Loaded {secret_name} from Secret Manager")
                return response.payload.data.decode("UTF-8")
            except Exception as e:
                # Get service account info for debugging
                creds_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "not set")
                logger.warning(
                    f"Failed to get secret {secret_name} from Secret Manager: {e}. "
                    f"GOOGLE_APPLICATION_CREDENTIALS={creds_file}, "
                    f"GCP_PROJECT_ID={self.gcp_project_id}"
                )

        # Provide helpful error message
        creds_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "not set")
        raise ValueError(
            f"Secret {secret_name} not found in environment or Secret Manager. "
            f"Ensure the secret exists in project '{self.gcp_project_id}' and the service account "
            f"(GOOGLE_APPLICATION_CREDENTIALS={creds_file}) has 'Secret Manager Secret Accessor' role."
        )

    def _get_access_token(self) -> str:
        """
        Get or refresh the access token.

        Returns:
            Valid access token
        """
        # Check if token is still valid (with 5 min buffer)
        if self._access_token and time.time() < (self._token_expires_at - 300):
            return self._access_token

        # Request new token
        token_url = self.TOKEN_URL.format(tenant_id=self.tenant_id)

        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }

        response = requests.post(token_url, data=data)
        response.raise_for_status()

        token_data = response.json()
        self._access_token = token_data["access_token"]
        self._token_expires_at = time.time() + token_data.get("expires_in", 3600)

        logger.debug("Access token refreshed")
        return self._access_token

    def _make_request(
        self,
        method: str,
        url: str,
        params: Optional[dict] = None,
        json_data: Optional[dict] = None,
        use_beta: bool = False,
    ) -> dict:
        """
        Make an authenticated request to Graph API with retry logic.

        Args:
            method: HTTP method
            url: Full URL or path (will be prefixed with base URL)
            params: Query parameters
            json_data: JSON body data
            use_beta: Use beta API endpoint

        Returns:
            JSON response
        """
        # Build full URL if needed
        if not url.startswith("http"):
            base = self.GRAPH_API_BETA if use_beta else self.GRAPH_API_BASE
            url = f"{base}{url}"

        headers = {
            "Authorization": f"Bearer {self._get_access_token()}",
            "Content-Type": "application/json",
        }

        for attempt in range(self.MAX_RETRIES):
            try:
                response = requests.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    json=json_data,
                    timeout=30,
                )

                # Handle rate limiting
                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", self.RETRY_DELAY * (attempt + 1)))
                    logger.warning(f"Rate limited. Retrying after {retry_after} seconds...")
                    time.sleep(retry_after)
                    continue

                response.raise_for_status()
                return response.json() if response.text else {}

            except requests.exceptions.RequestException as e:
                if attempt < self.MAX_RETRIES - 1:
                    logger.warning(f"Request failed (attempt {attempt + 1}): {e}")
                    time.sleep(self.RETRY_DELAY * (attempt + 1))
                else:
                    raise

        return {}

    def _paginate(
        self,
        url: str,
        params: Optional[dict] = None,
        use_beta: bool = False,
        max_items: Optional[int] = None,
    ) -> Generator[dict, None, None]:
        """
        Handle pagination for Graph API requests.

        Args:
            url: API endpoint URL
            params: Query parameters
            use_beta: Use beta API endpoint
            max_items: Maximum number of items to return (None for all)

        Yields:
            Individual items from paginated response
        """
        params = params or {}
        if "$top" not in params:
            params["$top"] = self.DEFAULT_PAGE_SIZE

        items_yielded = 0
        next_url = url

        while next_url:
            # For subsequent pages, use the full URL from @odata.nextLink
            if next_url.startswith("http"):
                response = self._make_request("GET", next_url, use_beta=use_beta)
            else:
                response = self._make_request("GET", next_url, params=params, use_beta=use_beta)
                params = None  # Clear params for subsequent requests

            items = response.get("value", [])

            for item in items:
                if max_items and items_yielded >= max_items:
                    return
                yield item
                items_yielded += 1

            # Get next page URL
            next_url = response.get("@odata.nextLink")

            if next_url:
                logger.debug(f"Fetching next page... ({items_yielded} items so far)")

    def get_site_id(self) -> str:
        """
        Get the SharePoint site ID.

        Returns:
            Site ID
        """
        if self._site_id:
            return self._site_id

        # Build site identifier
        if self.site_path:
            site_identifier = f"{self.site_hostname}:{self.site_path}"
        else:
            site_identifier = self.site_hostname

        url = f"/sites/{site_identifier}"
        response = self._make_request("GET", url)

        self._site_id = response["id"]
        logger.info(f"Site ID: {self._site_id}")

        return self._site_id

    # ==================== Site Pages ====================

    def get_site_pages(
        self,
        max_items: Optional[int] = None,
        select: Optional[list[str]] = None,
        filter_query: Optional[str] = None,
    ) -> Generator[dict, None, None]:
        """
        Get all site pages from SharePoint.

        Args:
            max_items: Maximum number of pages to return
            select: List of fields to select
            filter_query: OData filter query

        Yields:
            Site page objects
        """
        site_id = self.get_site_id()
        url = f"/sites/{site_id}/pages"

        params = {}
        if select:
            params["$select"] = ",".join(select)
        if filter_query:
            params["$filter"] = filter_query

        logger.info(f"Fetching site pages from {self.site_hostname}{self.site_path}")

        for page in self._paginate(url, params=params, use_beta=True, max_items=max_items):
            yield page

    def get_page_by_id(self, page_id: str) -> dict:
        """
        Get a specific site page by ID.

        Args:
            page_id: Page ID

        Returns:
            Page object
        """
        site_id = self.get_site_id()
        url = f"/sites/{site_id}/pages/{page_id}"

        return self._make_request("GET", url, use_beta=True)

    def get_page_content(self, page_id: str) -> dict:
        """
        Get the content (web parts) of a site page.

        Args:
            page_id: Page ID

        Returns:
            Page with web parts content
        """
        site_id = self.get_site_id()
        url = f"/sites/{site_id}/pages/{page_id}/microsoft.graph.sitePage/webParts"

        response = self._make_request("GET", url, use_beta=True)
        return response.get("value", [])

    def get_page_with_content(self, page_id: str) -> dict:
        """
        Get a site page with its full content expanded.

        Args:
            page_id: Page ID

        Returns:
            Page object with content
        """
        site_id = self.get_site_id()
        url = f"/sites/{site_id}/pages/{page_id}/microsoft.graph.sitePage"

        params = {
            "$expand": "canvasLayout"
        }

        return self._make_request("GET", url, params=params, use_beta=True)

    def get_all_pages_with_content(
        self,
        max_items: Optional[int] = None,
    ) -> Generator[dict, None, None]:
        """
        Get all site pages with their content.

        Args:
            max_items: Maximum number of pages to return

        Yields:
            Page objects with content
        """
        for page in self.get_site_pages(max_items=max_items):
            page_id = page.get("id")
            if page_id:
                try:
                    full_page = self.get_page_with_content(page_id)
                    full_page["webParts"] = self.get_page_content(page_id)
                    yield full_page
                except Exception as e:
                    logger.warning(f"Failed to get content for page {page_id}: {e}")
                    yield page

    # ==================== Lists ====================

    def get_lists(
        self,
        max_items: Optional[int] = None,
        select: Optional[list[str]] = None,
    ) -> Generator[dict, None, None]:
        """
        Get all lists from the SharePoint site.

        Args:
            max_items: Maximum number of lists to return
            select: List of fields to select

        Yields:
            List objects
        """
        site_id = self.get_site_id()
        url = f"/sites/{site_id}/lists"

        params = {}
        if select:
            params["$select"] = ",".join(select)

        logger.info(f"Fetching lists from {self.site_hostname}{self.site_path}")

        for lst in self._paginate(url, params=params, max_items=max_items):
            yield lst

    def get_list_by_id(self, list_id: str) -> dict:
        """
        Get a specific list by ID.

        Args:
            list_id: List ID

        Returns:
            List object
        """
        site_id = self.get_site_id()
        url = f"/sites/{site_id}/lists/{list_id}"

        return self._make_request("GET", url)

    def get_list_by_name(self, list_name: str) -> dict:
        """
        Get a specific list by display name.

        Args:
            list_name: List display name

        Returns:
            List object
        """
        for lst in self.get_lists():
            if lst.get("displayName") == list_name:
                return lst

        raise ValueError(f"List '{list_name}' not found")

    def get_list_items(
        self,
        list_id: str,
        max_items: Optional[int] = None,
        select: Optional[list[str]] = None,
        expand: Optional[list[str]] = None,
        filter_query: Optional[str] = None,
    ) -> Generator[dict, None, None]:
        """
        Get items from a SharePoint list.

        Args:
            list_id: List ID
            max_items: Maximum number of items to return
            select: List of fields to select
            expand: List of fields to expand
            filter_query: OData filter query

        Yields:
            List item objects
        """
        site_id = self.get_site_id()
        url = f"/sites/{site_id}/lists/{list_id}/items"

        params = {}
        if select:
            params["$select"] = ",".join(select)
        if expand:
            params["$expand"] = ",".join(expand)
        if filter_query:
            params["$filter"] = filter_query

        # Always expand fields to get column values
        if "$expand" not in params:
            params["$expand"] = "fields"

        logger.info(f"Fetching items from list {list_id}")

        for item in self._paginate(url, params=params, max_items=max_items):
            yield item

    def get_list_items_by_name(
        self,
        list_name: str,
        max_items: Optional[int] = None,
        select: Optional[list[str]] = None,
        expand: Optional[list[str]] = None,
        filter_query: Optional[str] = None,
    ) -> Generator[dict, None, None]:
        """
        Get items from a SharePoint list by list name.

        Args:
            list_name: List display name
            max_items: Maximum number of items to return
            select: List of fields to select
            expand: List of fields to expand
            filter_query: OData filter query

        Yields:
            List item objects
        """
        lst = self.get_list_by_name(list_name)
        list_id = lst["id"]

        yield from self.get_list_items(
            list_id=list_id,
            max_items=max_items,
            select=select,
            expand=expand,
            filter_query=filter_query,
        )

    def get_list_drive(self, list_id: str) -> dict:
        """
        Get the drive associated with a list (for document libraries).

        Args:
            list_id: List ID

        Returns:
            Drive object for the list
        """
        site_id = self.get_site_id()
        url = f"/sites/{site_id}/lists/{list_id}/drive"

        logger.info(f"Fetching drive for list {list_id}")

        return self._make_request("GET", url)

    def get_list_drive_root(self, list_id: str) -> dict:
        """
        Get the root folder of a list's drive (document library).

        Args:
            list_id: List ID

        Returns:
            Drive root object
        """
        site_id = self.get_site_id()
        url = f"/sites/{site_id}/lists/{list_id}/drive/root"

        logger.info(f"Fetching drive root for list {list_id}")

        return self._make_request("GET", url)

    def get_list_drive_children(
        self,
        list_id: str,
        folder_path: str = "",
        max_items: Optional[int] = None,
        recursive: bool = False,
    ) -> Generator[dict, None, None]:
        """
        Get children (files and folders) from a list's drive root.

        This is equivalent to: /sites/{SiteID}/lists/{ListID}/drive/root/children

        Args:
            list_id: List ID
            folder_path: Path to subfolder (empty for root)
            max_items: Maximum number of items to return
            recursive: Whether to recursively get items from subfolders

        Yields:
            Drive item objects (files and folders)
        """
        site_id = self.get_site_id()

        if folder_path:
            url = f"/sites/{site_id}/lists/{list_id}/drive/root:/{folder_path}:/children"
        else:
            url = f"/sites/{site_id}/lists/{list_id}/drive/root/children"

        logger.info(f"Fetching drive children for list {list_id}" + (f" at path '{folder_path}'" if folder_path else ""))

        items_yielded = 0

        for item in self._paginate(url, max_items=max_items):
            yield item
            items_yielded += 1

            # Recursively get items from folders
            if recursive and item.get("folder"):
                item_path = f"{folder_path}/{item['name']}" if folder_path else item["name"]
                remaining = (max_items - items_yielded) if max_items else None

                if remaining is None or remaining > 0:
                    for child in self.get_list_drive_children(
                        list_id=list_id,
                        folder_path=item_path,
                        max_items=remaining,
                        recursive=True,
                    ):
                        yield child
                        items_yielded += 1

                        if max_items and items_yielded >= max_items:
                            return

    def get_list_drive_item(
        self,
        list_id: str,
        item_path: str,
    ) -> dict:
        """
        Get a specific item from a list's drive by path.

        Args:
            list_id: List ID
            item_path: Path to the item

        Returns:
            Drive item object
        """
        site_id = self.get_site_id()
        url = f"/sites/{site_id}/lists/{list_id}/drive/root:/{item_path}"

        return self._make_request("GET", url)

    def get_list_drive_item_content(
        self,
        list_id: str,
        item_path: str,
    ) -> bytes:
        """
        Get the content of a file from a list's drive.

        Args:
            list_id: List ID
            item_path: Path to the file

        Returns:
            File content as bytes
        """
        site_id = self.get_site_id()
        url = f"{self.GRAPH_API_BASE}/sites/{site_id}/lists/{list_id}/drive/root:/{item_path}:/content"

        headers = {
            "Authorization": f"Bearer {self._get_access_token()}",
        }

        response = requests.get(url, headers=headers, timeout=60)
        response.raise_for_status()

        return response.content

    # ==================== Drive (Document Libraries) ====================

    def get_drive_root(self) -> dict:
        """
        Get the default document library (drive) root.

        Returns:
            Drive root object
        """
        site_id = self.get_site_id()
        url = f"/sites/{site_id}/drive/root"

        logger.info(f"Fetching drive root from {self.site_hostname}{self.site_path}")

        return self._make_request("GET", url)

    def get_drives(self) -> Generator[dict, None, None]:
        """
        Get all drives (document libraries) in the site.

        Yields:
            Drive objects
        """
        site_id = self.get_site_id()
        url = f"/sites/{site_id}/drives"

        for drive in self._paginate(url):
            yield drive

    def get_drive_items(
        self,
        drive_id: Optional[str] = None,
        folder_path: str = "",
        max_items: Optional[int] = None,
        recursive: bool = False,
    ) -> Generator[dict, None, None]:
        """
        Get items from a drive (document library).

        Args:
            drive_id: Drive ID (uses default drive if None)
            folder_path: Path to folder (empty for root)
            max_items: Maximum number of items to return
            recursive: Whether to recursively get items from subfolders

        Yields:
            Drive item objects
        """
        site_id = self.get_site_id()

        if drive_id:
            if folder_path:
                url = f"/sites/{site_id}/drives/{drive_id}/root:/{folder_path}:/children"
            else:
                url = f"/sites/{site_id}/drives/{drive_id}/root/children"
        else:
            if folder_path:
                url = f"/sites/{site_id}/drive/root:/{folder_path}:/children"
            else:
                url = f"/sites/{site_id}/drive/root/children"

        items_yielded = 0

        for item in self._paginate(url, max_items=max_items):
            yield item
            items_yielded += 1

            # Recursively get items from folders
            if recursive and item.get("folder"):
                item_path = f"{folder_path}/{item['name']}" if folder_path else item["name"]
                remaining = (max_items - items_yielded) if max_items else None

                if remaining is None or remaining > 0:
                    for child in self.get_drive_items(
                        drive_id=drive_id,
                        folder_path=item_path,
                        max_items=remaining,
                        recursive=True,
                    ):
                        yield child
                        items_yielded += 1

                        if max_items and items_yielded >= max_items:
                            return

    def get_file_content(
        self,
        drive_id: Optional[str] = None,
        item_id: Optional[str] = None,
        item_path: Optional[str] = None,
    ) -> bytes:
        """
        Get the content of a file.

        Args:
            drive_id: Drive ID (uses default drive if None)
            item_id: Item ID (either item_id or item_path required)
            item_path: Item path (either item_id or item_path required)

        Returns:
            File content as bytes
        """
        site_id = self.get_site_id()

        if item_id:
            if drive_id:
                url = f"{self.GRAPH_API_BASE}/sites/{site_id}/drives/{drive_id}/items/{item_id}/content"
            else:
                url = f"{self.GRAPH_API_BASE}/sites/{site_id}/drive/items/{item_id}/content"
        elif item_path:
            if drive_id:
                url = f"{self.GRAPH_API_BASE}/sites/{site_id}/drives/{drive_id}/root:/{item_path}:/content"
            else:
                url = f"{self.GRAPH_API_BASE}/sites/{site_id}/drive/root:/{item_path}:/content"
        else:
            raise ValueError("Either item_id or item_path must be provided")

        headers = {
            "Authorization": f"Bearer {self._get_access_token()}",
        }

        response = requests.get(url, headers=headers, timeout=60)
        response.raise_for_status()

        return response.content

    def download_file(
        self,
        output_path: str,
        drive_id: Optional[str] = None,
        item_id: Optional[str] = None,
        item_path: Optional[str] = None,
    ) -> str:
        """
        Download a file to local storage.

        Args:
            output_path: Local path to save the file
            drive_id: Drive ID (uses default drive if None)
            item_id: Item ID (either item_id or item_path required)
            item_path: Item path (either item_id or item_path required)

        Returns:
            Path to downloaded file
        """
        content = self.get_file_content(
            drive_id=drive_id,
            item_id=item_id,
            item_path=item_path,
        )

        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        with open(output_path, "wb") as f:
            f.write(content)

        logger.info(f"Downloaded file to {output_path}")
        return output_path

    # ==================== Utility Methods ====================

    def search(
        self,
        query: str,
        entity_types: Optional[list[str]] = None,
        max_items: int = 100,
    ) -> list[dict]:
        """
        Search for content in SharePoint.

        Args:
            query: Search query
            entity_types: Entity types to search (e.g., ['driveItem', 'listItem', 'site'])
            max_items: Maximum number of results

        Returns:
            List of search results
        """
        url = "/search/query"

        entity_types = entity_types or ["driveItem", "listItem"]

        body = {
            "requests": [
                {
                    "entityTypes": entity_types,
                    "query": {
                        "queryString": query
                    },
                    "from": 0,
                    "size": min(max_items, 500),
                }
            ]
        }

        response = self._make_request("POST", url, json_data=body)

        results = []
        for hit_container in response.get("value", []):
            for hit in hit_container.get("hitsContainers", []):
                for result in hit.get("hits", []):
                    results.append(result)

        return results[:max_items]

    def get_site_info(self) -> dict:
        """
        Get detailed site information.

        Returns:
            Site information
        """
        site_id = self.get_site_id()
        url = f"/sites/{site_id}"

        return self._make_request("GET", url)

