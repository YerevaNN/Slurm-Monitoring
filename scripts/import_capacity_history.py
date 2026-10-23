"""Import reviewed capacity snapshots; run against a backed-up local database."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import history

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--snapshots', required=True)
    args = parser.parse_args()
    snapshots = json.loads(Path(args.snapshots).read_text())
    history.init_db(args.db)
    history.import_capacity_history(args.db, snapshots)
    print(f'Imported/verified {len(snapshots)} evidenced snapshots')
