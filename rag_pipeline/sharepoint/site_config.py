"""
SharePoint site configuration management.

Supports multiple site configurations via environment variables:
- Default site: SHAREPOINT_SITE_HOSTNAME, SHAREPOINT_SITE_PATH
- Named sites: SHAREPOINT_SITE_{NAME}_HOSTNAME, SHAREPOINT_SITE_{NAME}_PATH

Credentials per site (optional — falls back to global SHAREPOINT_* vars / Secret Manager):
- SHAREPOINT_SITE_{NAME}_TENANT_ID
- SHAREPOINT_SITE_{NAME}_CLIENT_ID
- SHAREPOINT_SITE_{NAME}_CLIENT_SECRET

Content source configuration:
- SHAREPOINT_SITE_{NAME}_CONTENT_SOURCE: "site_pages" (default) or "document_library"
- SHAREPOINT_SITE_{NAME}_LIBRARY_PREFIXES: Comma-separated library name prefixes (e.g., "Library 1,Library 2")
- SHAREPOINT_SITE_{NAME}_EXTERNAL_URLS_DRIVE: Optional drive name for external URLs file
- SHAREPOINT_SITE_{NAME}_EXTERNAL_URLS_FILE: Optional external URLs file name
- SHAREPOINT_SITE_{NAME}_APPROVAL_FIELD: Optional list field name for approval status

Example .env configuration:
    # Default site
    SHAREPOINT_SITE_HOSTNAME=contoso.sharepoint.com
    SHAREPOINT_SITE_PATH=/sites/MainSite

    # Additional named site with its own Azure AD credentials (different tenant)
    SHAREPOINT_SITE_SOM_HOSTNAME=other.sharepoint.com
    SHAREPOINT_SITE_SOM_PATH=/teams/SomGroup
    SHAREPOINT_SITE_SOM_TENANT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
    SHAREPOINT_SITE_SOM_CLIENT_ID=yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy
    SHAREPOINT_SITE_SOM_CLIENT_SECRET=secret_value
"""

import os
from typing import Optional, List
from dataclasses import dataclass
from rag_pipeline.utils.logger import setup_logger

logger = setup_logger()


@dataclass
class SiteConfig:
    """SharePoint site configuration."""
    name: str
    hostname: str
    path: str
    content_source: str = "site_pages"
    library_prefixes: Optional[List[str]] = None
    external_urls_drive: Optional[str] = None
    external_urls_file: Optional[str] = None
    approval_field: Optional[str] = None
    # Optional per-site Azure AD credentials.
    # If None, SharePointGraphClient falls back to global env vars / Secret Manager.
    tenant_id: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None

    @property
    def full_url(self) -> str:
        """Get the full site URL."""
        return f"https://{self.hostname}{self.path}"

    def __repr__(self):
        return f"SiteConfig(name='{self.name}', url='{self.full_url}')"


class SiteConfigManager:
    """
    Manages SharePoint site configurations from environment variables.

    Supports:
    - Default site configuration
    - Multiple named site configurations
    - Runtime site selection
    """

    def __init__(self):
        self._sites: dict[str, SiteConfig] = {}
        self._load_sites()

    @staticmethod
    def _parse_csv(value: str) -> List[str]:
        return [item.strip() for item in value.split(",") if item.strip()]

    def _load_sites(self):
        """Load all site configurations from environment variables."""
        # Load default site
        default_hostname = os.getenv("SHAREPOINT_SITE_HOSTNAME", "").strip()
        default_path = os.getenv("SHAREPOINT_SITE_PATH", "").strip()
        default_content_source = os.getenv("SHAREPOINT_SITE_CONTENT_SOURCE", "site_pages").strip() or "site_pages"
        default_library_prefixes = self._parse_csv(os.getenv("SHAREPOINT_SITE_LIBRARY_PREFIXES", ""))
        default_external_urls_drive = os.getenv("SHAREPOINT_SITE_EXTERNAL_URLS_DRIVE", "").strip() or None
        default_external_urls_file = os.getenv("SHAREPOINT_SITE_EXTERNAL_URLS_FILE", "").strip() or None
        default_approval_field = os.getenv("SHAREPOINT_SITE_APPROVAL_FIELD", "").strip() or None

        if default_hostname:
            self._sites["default"] = SiteConfig(
                name="default",
                hostname=default_hostname,
                path=default_path,
                content_source=default_content_source.lower(),
                library_prefixes=default_library_prefixes or None,
                external_urls_drive=default_external_urls_drive,
                external_urls_file=default_external_urls_file,
                approval_field=default_approval_field,
            )
            logger.info(f"Loaded default SharePoint site: {default_hostname}{default_path}")

        # Scan for named sites (SHAREPOINT_SITE_{NAME}_HOSTNAME pattern)
        for key, value in os.environ.items():
            if key.startswith("SHAREPOINT_SITE_") and key.endswith("_HOSTNAME"):
                # Extract site name
                # SHAREPOINT_SITE_HR_HOSTNAME -> HR
                site_name = key[16:-9]  # Remove prefix and suffix

                if site_name and site_name.upper() != "HOSTNAME":
                    hostname = value.strip()
                    path_key = f"SHAREPOINT_SITE_{site_name}_PATH"
                    path = os.getenv(path_key, "").strip()
                    content_source = os.getenv(
                        f"SHAREPOINT_SITE_{site_name}_CONTENT_SOURCE", "site_pages"
                    ).strip() or "site_pages"
                    library_prefixes = self._parse_csv(
                        os.getenv(f"SHAREPOINT_SITE_{site_name}_LIBRARY_PREFIXES", "")
                    )
                    external_urls_drive = os.getenv(
                        f"SHAREPOINT_SITE_{site_name}_EXTERNAL_URLS_DRIVE", ""
                    ).strip() or None
                    external_urls_file = os.getenv(
                        f"SHAREPOINT_SITE_{site_name}_EXTERNAL_URLS_FILE", ""
                    ).strip() or None
                    approval_field = os.getenv(
                        f"SHAREPOINT_SITE_{site_name}_APPROVAL_FIELD", ""
                    ).strip() or None

                    if hostname:
                        normalized_name = site_name.lower()
                        self._sites[normalized_name] = SiteConfig(
                            name=normalized_name,
                            hostname=hostname,
                            path=path,
                            tenant_id=os.getenv(f"SHAREPOINT_SITE_{site_name}_TENANT_ID", "").strip() or None,
                            client_id=os.getenv(f"SHAREPOINT_SITE_{site_name}_CLIENT_ID", "").strip() or None,
                            client_secret=os.getenv(f"SHAREPOINT_SITE_{site_name}_CLIENT_SECRET", "").strip() or None,
                            content_source=content_source.lower(),
                            library_prefixes=library_prefixes or None,
                            external_urls_drive=external_urls_drive,
                            external_urls_file=external_urls_file,
                            approval_field=approval_field,
                        )
                        logger.info(f"Loaded SharePoint site '{normalized_name}': {hostname}{path}")

    def get_site(self, name: Optional[str] = None) -> SiteConfig:
        """
        Get a site configuration by name.

        Args:
            name: Site name (None or 'default' for default site)

        Returns:
            SiteConfig for the requested site

        Raises:
            ValueError: If site not found
        """
        name = (name or "default").lower().strip()

        if name not in self._sites:
            available = list(self._sites.keys())
            raise ValueError(
                f"SharePoint site '{name}' not configured. "
                f"Available sites: {available}"
            )

        return self._sites[name]

    def get_default_site(self) -> SiteConfig:
        """Get the default site configuration."""
        return self.get_site("default")

    def list_sites(self) -> list[SiteConfig]:
        """List all configured sites."""
        return list(self._sites.values())

    def list_site_names(self) -> list[str]:
        """List all configured site names."""
        return list(self._sites.keys())

    def has_site(self, name: str) -> bool:
        """Check if a site is configured."""
        return name.lower().strip() in self._sites

    def reload(self):
        """Reload site configurations from environment."""
        self._sites.clear()
        self._load_sites()


# Global site config manager instance
_site_config_manager: Optional[SiteConfigManager] = None


def get_site_config_manager() -> SiteConfigManager:
    """Get the global site configuration manager."""
    global _site_config_manager
    if _site_config_manager is None:
        _site_config_manager = SiteConfigManager()
    return _site_config_manager


def get_site_config(name: Optional[str] = None) -> SiteConfig:
    """
    Get a site configuration by name.

    Args:
        name: Site name (None for default)

    Returns:
        SiteConfig
    """
    return get_site_config_manager().get_site(name)
