"""Resolve explicitly configured existing credential files in memory only."""
from __future__ import annotations
import json,re,shlex
from pathlib import Path
from . import Redactor

def redactor_from_references(references=()):
    values=[]
    for ref in references:
        try:
            path=Path(ref["file"])
            if ref["format"] in ("json","yaml"):
                if ref["format"]=="yaml":
                    import yaml  # Optional redaction-yaml extra; never load for core-only use.
                    value=yaml.safe_load(path.read_text())
                else:value=json.loads(path.read_text())
                for key in ref["path"]:value=value[key]
                selected=[value]
            elif ref["format"]=="dotenv":
                parsed={}
                for line in path.read_text().splitlines():
                    line=line.strip()
                    if not line or line.startswith("#"):continue
                    if line.startswith("export "):line=line[7:].lstrip()
                    key,sep,value=line.partition("=")
                    if not sep or key.strip() not in ref["names"]:continue
                    value=value.strip()
                    if value.startswith(("'",'"')):
                        tokens=shlex.split(value,comments=True,posix=True)
                        if len(tokens)!=1:raise ValueError()
                        value=tokens[0]
                    else:value=re.split(r"\s+#",value,maxsplit=1)[0].rstrip()
                    parsed[key.strip()]=value
                selected=[parsed[key] for key in ref["names"]]
            else:raise ValueError()
            for value in selected:
                if not isinstance(value,str) or not value or ("$"+"{") in value:raise ValueError()
                values.append(value)
        except Exception:
            raise ValueError("REDACTION_REFERENCE_UNAVAILABLE") from None
    return Redactor(known_secrets=values)

def redactor_from_config(path):
    try:refs=json.loads(Path(path).read_text()).get("redaction_secret_refs",[])
    except Exception:raise ValueError("REDACTION_REFERENCE_UNAVAILABLE") from None
    return redactor_from_references(refs)
