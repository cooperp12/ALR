"""Inspect ALR legacy compatibility's Investigation Frame and semantic bindings without Splunk."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))

from commander_agent.semantic.frame import heuristic_frame, render_investigation_frame
from commander_agent.semantic.binding import resolve_semantic_bindings


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--question')
    args=ap.parse_args()
    q=(args.question or input('Question: ')).strip()
    if not q: return 2
    frame=heuristic_frame(q)
    bindings=resolve_semantic_bindings(frame,q)
    print('\nINVESTIGATION FRAME')
    print(render_investigation_frame(frame))
    print('\nSEMANTIC BINDINGS')
    print(json.dumps(bindings,ensure_ascii=False,indent=2))
    return 0

if __name__=='__main__': raise SystemExit(main())
