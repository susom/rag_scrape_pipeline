#!/usr/bin/env python
"""
One-time maintenance: republish RAG-tagged SharePoint pages that were bumped to a
pending *draft* version by the act of checking the "RAG Worthy?" column.

Background
----------
Editing a list column (e.g. "RAG Worthy?") creates a new *minor draft* version
(X.1) on top of the live *published major* (X.0). The modern /pages API then reports
publishingState.level == "draft" for those pages even though a published version is
still live to visitors. This script promotes those drafts back to published so the
library returns to a clean state.

Selection
---------
A page is a candidate when ALL of:
  - its RAG curation column (default RAGWorthy_x003f_) is truthy
  - it has been published before (FirstPublishedDate is set)
  - its latest version is currently a draft (publishingState.level == "draft")

Safety
------
  - DRY RUN by default. Pass --apply to actually publish.
  - WARNING: publishing promotes ALL pending changes on a page, not just the
    checkbox. Review the dry-run output for pages that may have unfinished content
    edits before applying.

Usage
-----
  python scripts/republish_tagged_pages.py --site som
  python scripts/republish_tagged_pages.py --site som --apply
  python scripts/republish_tagged_pages.py --site som --apply --limit 10
"""
import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env from repo root regardless of where this is invoked from.
_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env")
sys.path.insert(0, str(_REPO_ROOT))

from rag_pipeline.sharepoint import SharePointGraphClient  # noqa: E402
from rag_pipeline.sharepoint.site_config import SiteConfigManager  # noqa: E402


def build_client(site_name: str) -> tuple[SharePointGraphClient, str]:
    cfg = SiteConfigManager().get_site(site_name)
    if not cfg.rag_filter_column:
        raise SystemExit(
            f"Site '{site_name}' has no rag_filter_column configured "
            f"(SHAREPOINT_SITE_{site_name.upper()}_RAG_FILTER_COLUMN)."
        )
    client = SharePointGraphClient(
        site_hostname=cfg.hostname,
        site_path=cfg.path,
        tenant_id=cfg.tenant_id,
        client_id=cfg.client_id,
        client_secret=cfg.client_secret,
    )
    return client, cfg.rag_filter_column


def find_candidates(client: SharePointGraphClient, rag_column: str) -> list[dict]:
    # Modern pages API: name -> page identity + latest publishing level
    pages_by_name: dict[str, dict] = {}
    for p in client.get_site_pages(
        select=["id", "title", "name", "webUrl", "publishingState"]
    ):
        ps = p.get("publishingState") or {}
        pages_by_name[p.get("name")] = {
            "id": p.get("id"),
            "title": p.get("title"),
            "name": p.get("name"),
            "url": p.get("webUrl"),
            "level": ps.get("level"),
        }

    # List items: RAG flag + first-published + version, keyed by FileLeafRef (== name)
    field_map = client.get_site_pages_field_map(
        [rag_column, "FirstPublishedDate", "_UIVersionString"]
    )

    candidates = []
    for name, fields in field_map.items():
        if not bool(fields.get(rag_column)):
            continue
        if not fields.get("FirstPublishedDate"):
            continue
        page = pages_by_name.get(name)
        if not page or page.get("level") != "draft":
            continue
        candidates.append({**page, "version": fields.get("_UIVersionString")})
    return candidates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", default="som", help="Site name (default: som)")
    parser.add_argument("--apply", action="store_true", help="Actually publish (default: dry run)")
    parser.add_argument("--limit", type=int, default=0, help="Max pages to publish (0 = no limit)")
    parser.add_argument(
        "--comment",
        default="Republished after RAG curation tagging",
        help="Publish comment",
    )
    args = parser.parse_args()

    client, rag_column = build_client(args.site)
    print(f"Site: {args.site} | RAG column: {rag_column}")
    print("Scanning for tagged pages stuck in draft...\n")

    candidates = find_candidates(client, rag_column)
    if args.limit:
        candidates = candidates[: args.limit]

    if not candidates:
        print("No tagged-and-draft pages found. Nothing to republish.")
        return 0

    print(f"{len(candidates)} page(s) tagged RAG-worthy and currently in draft:\n")
    for c in candidates:
        print(f"  v{c['version']:<5} {c['title']}")

    if not args.apply:
        print("\nDRY RUN — no changes made. Re-run with --apply to publish these pages.")
        return 0

    print("\nPublishing...")
    ok, failed, unchanged = 0, 0, 0
    for c in candidates:
        try:
            client.publish_page(c["id"], comment=args.comment)
            # The 204 from Graph does NOT guarantee the draft was promoted — verify.
            after = client.get_page_by_id(c["id"])
            level = (after.get("publishingState") or {}).get("level")
            if level == "published":
                ok += 1
                print(f"  ✅ {c['title']}")
            else:
                unchanged += 1
                print(f"  ⚠️  {c['title']} — still '{level}' (publish was a no-op)")
        except Exception as e:
            failed += 1
            print(f"  ❌ {c['title']}: {e}")

    print(f"\nDone: {ok} published, {unchanged} unchanged (no-op), {failed} failed.")
    if unchanged and not ok:
        print(
            "\nNOTE: Graph's publish action does not clear drafts created by editing a\n"
            "list column (the page-canvas and list-item version tracks differ). Clearing\n"
            "these requires the SharePoint REST file Publish API, which rejects this app's\n"
            "app-only token ('Unsupported app only token'). Ingestion is unaffected — the\n"
            "pipeline gates on FirstPublishedDate, not the latest version's publish level."
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
