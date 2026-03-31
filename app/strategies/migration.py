"""
Strategy name harmonization migration runner.

This module provides utilities to migrate from legacy kebab-case strategy IDs
to canonical snake_case names across all persisted data.
"""

import logging
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)


# Legacy to canonical mappings (same as in SQL)
LEGACY_TO_CANONICAL: Dict[str, str] = {
    "ng-options": "option_strategy",
    "nifty-options": "option_strategy",
    "banknifty-options": "option_strategy",
    "finnifty-options": "option_strategy",
    "sensex-options": "option_strategy",
    "midcpnifty-options": "option_strategy",
    "nifty-delta-strangle": "delta_strangle",
    "banknifty-delta-strangle": "delta_strangle",
    "ema20-trend": "ema20_strategy",
    "ce-orb": "ce_orb",
    "ce-pdh-rb": "ce_pdh_rb",
    "put-momentum-scalper": "put_momentum_scalper",
    "put-reversion-writer": "put_reversion_writer",
    "nifty-weekly-credit-spreads": "nifty_weekly_credit_spreads",
    "exclusive-nifty-ce-buy": "exclusive_nifty_ce_buy",
    "put-buy": "put_buy",
}

# Reverse mapping for validation
CANONICAL_TO_LEGACY: Dict[str, List[str]] = {}
for legacy, canonical in LEGACY_TO_CANONICAL.items():
    if canonical not in CANONICAL_TO_LEGACY:
        CANONICAL_TO_LEGACY[canonical] = []
    CANONICAL_TO_LEGACY[canonical].append(legacy)


def migrate_strategy_id(strategy_id: str) -> str:
    """
    Convert a legacy kebab-case strategy_id to canonical snake_case name.
    
    Args:
        strategy_id: Strategy identifier (may be legacy or canonical)
        
    Returns:
        Canonical strategy name.
        
    Raises:
        ValueError: If strategy_id is not recognized.
    """
    strategy_id = str(strategy_id or "").strip()
    
    # Already canonical?
    if strategy_id in CANONICAL_TO_LEGACY:
        return strategy_id
    
    # Look up legacy ID
    if strategy_id in LEGACY_TO_CANONICAL:
        canonical = LEGACY_TO_CANONICAL[strategy_id]
        logger.info(
            "Migrating strategy_id: %s → %s",
            strategy_id,
            canonical,
        )
        return canonical
    
    # Unknown ID
    raise ValueError(
        f"Unknown strategy_id '{strategy_id}'. "
        f"Valid canonical names: {sorted(set(CANONICAL_TO_LEGACY.keys()))}"
    )


async def migrate_strategy_configs_firestore(
    firestore_client,
) -> Tuple[int, int]:
    """
    Migrate strategy configs in Firestore from legacy to canonical names.
    
    Args:
        firestore_client: Firestore client instance.
        
    Returns:
        Tuple of (total_migrated, total_errors).
    """
    migrated = 0
    errors = 0
    
    logger.info("Starting Firestore strategy_config migration...")
    
    try:
        # Query all strategy_config documents
        docs = firestore_client.collection("strategy_config").stream()
        
        for doc in docs:
            try:
                data = doc.to_dict()
                if not data:
                    continue
                
                old_strategy_id = data.get("strategy_id", "")
                new_strategy_id = migrate_strategy_id(old_strategy_id)
                
                if old_strategy_id != new_strategy_id:
                    # Update the document
                    doc.reference.update({
                        "strategy_id": new_strategy_id,
                        "migrated_at": firestore_client._firestore.Timestamp.now(),
                        "legacy_strategy_id": old_strategy_id,  # Keep for audit
                    })
                    migrated += 1
                    logger.debug(
                        "Migrated Firestore doc %s: %s → %s",
                        doc.id,
                        old_strategy_id,
                        new_strategy_id,
                    )
            except Exception as e:
                errors += 1
                logger.error(
                    "Error migrating Firestore doc %s: %s",
                    doc.id if hasattr(doc, "id") else "unknown",
                    e,
                )
    
    except Exception as e:
        logger.error("Fatal error during Firestore strategy_config migration: %s", e)
        raise
    
    logger.info(
        "Firestore strategy_config migration complete: "
        "%d migrated, %d errors",
        migrated,
        errors,
    )
    return migrated, errors


def migrate_strategy_id_in_dict(data: dict, field_name: str = "strategy_id") -> dict:
    """
    Update a dictionary's strategy_id field from legacy to canonical.
    
    Args:
        data: Dictionary to update.
        field_name: Name of the field to migrate (default: "strategy_id").
        
    Returns:
        Updated dictionary (modifies in place and returns).
    """
    if field_name in data:
        old_id = data[field_name]
        try:
            data[field_name] = migrate_strategy_id(old_id)
            if old_id != data[field_name]:
                data[f"_{field_name}_legacy"] = old_id  # Audit trail
        except ValueError as e:
            logger.warning("Cannot migrate %s=%s: %s", field_name, old_id, e)
    
    return data


def migrate_strategy_ids_in_list(
    items: List[dict],
    field_name: str = "strategy_id",
) -> List[dict]:
    """
    Migrate strategy_id fields in a list of dictionaries.
    
    Args:
        items: List of dictionaries.
        field_name: Name of the field to migrate.
        
    Returns:
        List of migrated dictionaries.
    """
    return [
        migrate_strategy_id_in_dict(item.copy(), field_name)
        for item in items
    ]


def get_migration_summary() -> Dict[str, any]:
    """Get a summary of the migration mapping for documentation."""
    return {
        "legacy_to_canonical": LEGACY_TO_CANONICAL,
        "canonical_strategies": sorted(set(CANONICAL_TO_LEGACY.keys())),
        "total_legacy_aliases": len(LEGACY_TO_CANONICAL),
        "migration_complete": True,
    }
