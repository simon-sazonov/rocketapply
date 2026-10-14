"""One generation, in a process of its own.

`runner.start_generation` starts this in a new session, so the run outlives
the Streamlit server: a restart, a closed terminal or a host that stops idle
servers ends the page, not the work. It talks to the page only through the run
folder — every event appended to `events.jsonl`, a heartbeat and its pid in
`meta.json` — which the page follows with `runner.sync`.

    python apps/rocketapply/rocket/worker.py <run_id>     (from the repo root)
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))


def main(run_id: str) -> int:
    from pitwall.apply.batch import run_batch
    from rocket.runner import RUN_ROOT, GenerationRun

    meta = json.loads((RUN_ROOT / run_id / "meta.json").read_text(encoding="utf-8"))
    run = GenerationRun(run_id=run_id, job_ids=[int(j) for j in meta["job_ids"]],
                        mode=meta.get("mode", "cv_letter"),
                        hints={int(k): v for k, v in (meta.get("hints") or {}).items()},
                        started=float(meta.get("started") or time.time()), pid=os.getpid(),
                        workers=int(meta.get("workers") or 3), fresh=bool(meta.get("fresh")))
    run.write_meta()
    try:
        run_batch(
            run.job_ids, name=f"rocketapply-{run_id}",
            letter=run.mode != "cv", audit=run.mode == "cv_letter_audit",
            workers=max(1, run.workers), resume=True, record=True,
            on_event=run.on_event, hints=run.hints or None, fresh=run.fresh,
        )
        run.status = "done"
    except Exception as exc:  # noqa: BLE001 - shown on the Generate page
        run.status, run.error = "error", f"{exc}\n{traceback.format_exc(limit=3)}"
    finally:
        run.finished = time.time()
        run.write_meta()
    return 0 if run.status == "done" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
