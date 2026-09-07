"""补回 2025-10-15 缺失的 daily_kline 数据 (从 baostock)."""
import sqlite3, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from alpha_agents.data.market_history import (
    _fetch_batch, _save_batch, _get_conn,
)

DATE = '2025-10-15'

def main():
    conn = _get_conn()
    # All codes already in db
    all_codes = [r[0] for r in conn.execute('SELECT DISTINCT code FROM daily_kline').fetchall()]
    print(f'Total codes in db: {len(all_codes)}')

    # Codes that already have 2025-10-15
    have_codes = {r[0] for r in conn.execute(
        'SELECT DISTINCT code FROM daily_kline WHERE date=?', (DATE,)
    ).fetchall()}
    print(f'Codes with {DATE} data: {len(have_codes)}')

    missing = [c for c in all_codes if c not in have_codes]
    print(f'Missing: {len(missing)}')

    if not missing:
        print('No backfill needed.')
        return

    print(f'Fetching {DATE} from baostock for {len(missing)} codes...', flush=True)
    rows = _fetch_batch(missing, DATE, DATE, progress_every=500)
    # Filter only target date rows
    rows_target = [r for r in rows if r[1] == DATE]
    print(f'Got {len(rows_target)} rows for {DATE}')

    if rows_target:
        n = _save_batch(rows_target)
        print(f'Saved.')

    # Verify
    new_n = conn.execute('SELECT COUNT(DISTINCT code) FROM daily_kline WHERE date=?', (DATE,)).fetchone()[0]
    print(f'After backfill: {new_n} codes for {DATE}')

if __name__ == '__main__':
    main()
