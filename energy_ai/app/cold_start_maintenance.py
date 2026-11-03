from __future__ import annotations

import json

from .db_compaction import compact_before_server_start
from .retired_models import remove_retired_model_artifacts


def main() -> int:
    result = compact_before_server_start()
    print('cold_compaction: ' + json.dumps(result), flush=True)
    if result.get('status') == 'database_missing':
        return 1
    print('retired_models: ' + json.dumps(remove_retired_model_artifacts()), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
