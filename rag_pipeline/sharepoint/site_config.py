"""
SharePoint site configuration management.

Supports multiple site configurations via environment variables:
- Default site: SHAREPOINT_SITE_HOSTNAME, SHAREPOINT_SITE_PATH
- Named sites: SHAREPOINT_SITE_{NAME}_HOSTNAME, SHAREPOINT_SITE_{NAME}_PATH

Example .env configuration:
    # Default site
    SHAREPOINT_SITE_HOSTNAME=contoso.sharepoint.com
    SHAREPOINT_SITE_PATH=/sites/MainSite

    # Additional named sites
    SHAREPOINT_SITE_HR_HOSTNAME=contoso.sharepoint.com
    SHAREPOINT_SITE_HR_PATH=/sites/HumanResources

    SHAREPOINT_SITE_FINANCE_HOSTNAME=contoso.sharepoint.com
    SHAREPOINT_SITE_FINANCE_PATH=/sites/Finance
"""

import os
from typing import Optional
from dataclasses import dataclass
from rag_pipeline.utils.logger import setup_logger

logger = setup_logger()


@dataclass
class SiteConfig:
    """SharePoint site configuration."""
    name: str
    hostname: str
    path: str

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

    def _load_sites(self):
        """Load all site configurations from environment variables."""
        # Load default site
        default_hostname = os.getenv("SHAREPOINT_SITE_HOSTNAME", "").strip()
        default_path = os.getenv("SHAREPOINT_SITE_PATH", "").strip()

        if default_hostname:
            self._sites["default"] = SiteConfig(
                name="default",
                hostname=default_hostname,
                path=default_path,
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

                    if hostname:
                        normalized_name = site_name.lower()
                        self._sites[normalized_name] = SiteConfig(
                            name=normalized_name,
                            hostname=hostname,
                            path=path,
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

