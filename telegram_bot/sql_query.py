"""Backward-compatible query interface — delegates to the database module.

Kept as a thin wrapper so that any code still importing from ``sql_query``
continues to work.  New code should import from ``database`` directly.
"""

from typing import Any, Dict, List, Optional, Sequence, Union

from psycopg2 import sql

from database import fetch_all


def query(
    script: Union[str, sql.Composable],
    params: Optional[Sequence] = None,
    columns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Execute *script* and return ``{column: [values…], 'count_rows': N}``."""
    return fetch_all(script, params, columns)
