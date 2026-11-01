"""Host-owned project aliases/defaults; never infer an arbitrary backend scope."""
import re

class QueryScopes:
    def __init__(self, defaults=(), *, aliases=None, allowed=None, allow_workspace=False):
        self.defaults=tuple(defaults)
        self.aliases=dict(aliases or {})
        self.allowed=frozenset(allowed if allowed is not None else self.defaults)
        self.allow_workspace=bool(allow_workspace)
        if not set(self.defaults)<=self.allowed or not set(self.aliases.values())<=self.allowed:
            raise ValueError("project aliases/defaults must belong to configured allowed projects")
        if any(not isinstance(k,str) or len(k)<2 for k in self.aliases):
            raise ValueError("project aliases must contain at least two characters")

    def resolve(self, query, selected=None):
        if selected is not None:
            if not isinstance(selected,list) or len(selected)>32 or any(not isinstance(p,str) or p not in self.allowed for p in selected):
                raise ValueError("project selection is outside configured scope")
            return tuple(dict.fromkeys(selected))
        hits=set()
        for alias,project in self.aliases.items():
            pattern=re.escape(alias)
            if re.fullmatch(r"[a-zA-Z0-9_.-]+",alias):
                pattern=r"(?<![a-zA-Z0-9_])"+pattern+r"(?![a-zA-Z0-9_])"
            if re.search(pattern,query,re.IGNORECASE):hits.add(project)
        if len(hits)>1:return ()  # require explicit selection; don't mix candidates
        return tuple(hits) if hits else self.defaults
