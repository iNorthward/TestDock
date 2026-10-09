#!/usr/bin/env python3
"""Run one explicitly isolated readonly case in a fresh selected-Pack process."""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'test-platform'),str(ROOT/'scripts')]
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--case',required=True);parser.add_argument('--input',required=True);parser.add_argument('--output',required=True);args=parser.parse_args()
    import catalog
    from identity_pool import redact_secrets
    options=json.loads(Path(args.input).read_text())
    result=catalog.exec_case(args.case,execution_options=options)
    Path(args.output).write_text(json.dumps(redact_secrets(result),ensure_ascii=False,allow_nan=False))
if __name__=='__main__':main()
