#!/usr/bin/env python3
"""Detect source-project residue without reading runtime secrets or Git history."""
from __future__ import annotations
import argparse,json,re
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
# Split strings prevent this check from flagging its own policy definition.
FORBIDDEN=('m'+'lt','mul'+'let','blade'+'x','qmz'+'1217','/Users/'+'remainder')
EXCLUDED={'.git','.idea','.run','data','artifacts','__pycache__','.venv','node_modules'}
def findings(root=ROOT):
    result=[]
    for p in sorted(Path(root).rglob('*')):
        rel=p.relative_to(root)
        if any(part in EXCLUDED for part in rel.parts) or not p.is_file():continue
        if any(token in rel.as_posix().lower() for token in FORBIDDEN):result.append({'file':rel.as_posix(),'kind':'legacy_filename'})
        if p.name=='.env.local' or p.name.startswith('.env.') and p.name!='.env.example':continue
        try:text=p.read_text(encoding='utf-8')
        except UnicodeError:continue
        for line_number,line in enumerate(text.splitlines(),1):
            if any(token in line.lower() for token in FORBIDDEN):result.append({'file':rel.as_posix(),'line':line_number,'kind':'legacy_identity'})
        for match in re.finditer(r'\[[^\]]*\]\(([^)]+)\)',text):
            target=match.group(1).split('#',1)[0]
            if p.suffix=='.md' and target and not re.match(r'^[a-z]+://',target) and not (p.parent/target).exists():
                result.append({'file':rel.as_posix(),'kind':'broken_local_link','target':target})
    return result
if __name__=='__main__':
    a=argparse.ArgumentParser(description=__doc__);a.add_argument('--json',action='store_true');args=a.parse_args();items=findings()
    print(json.dumps({'ok':not items,'findings':items,'scope':'distributed text and documentation links; excludes local IDE, runtime, secrets, Git metadata'},ensure_ascii=False,indent=2));raise SystemExit(bool(items))
