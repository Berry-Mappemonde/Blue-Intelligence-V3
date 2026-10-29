CATEGORY_GROUPS = {
    "MPA": "#00f0ff",
    "Conservation": "#39ff14",
    "Research": "#c084fc",
    "Fisheries": "#fbbf24",
    "Policy & Advocacy": "#f472b6",
    "Pollution": "#ff4a4a",
    "Coastal & Habitat": "#34d399",
    "Education": "#60a5fa",
    "Other": "#94a3b8",
}

_RULES = [
    ("MPA", ("protected area", "mpa", "hope spot")),
    ("Fisheries", ("fisher", "bycatch", "aquaculture", "fishing")),
    ("Policy & Advocacy", ("policy", "advocacy", "legislation", "governance", "law")),
    ("Pollution", ("pollution", "water quality", "plastic", "debris", "waste", "spill")),
    ("Education", ("educat", "awareness", "outreach", "engagement", "citizen")),
    ("Research", ("research", "observ", "science", "monitor", "data", "expedition", "survey")),
    ("Coastal & Habitat", ("coastal", "habitat", "restor", "beach", "mangrove", "reef", "coral",
                           "seagrass", "kelp", "island", "estuar", "blue carbon", "wetland")),
    ("Conservation", ("conserv", "species", "wildlife", "biodivers", "protection", "whale",
                      "shark", "turtle", "seabird", "endangered")),
]


def normalize_category(raw) -> str:
    if not raw:
        return "Other"
    low = str(raw).lower()
    for group, keywords in _RULES:
        if any(k in low for k in keywords):
            return group
    return "Other"
