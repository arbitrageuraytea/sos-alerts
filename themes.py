"""Theme keyword tagging from yfinance longBusinessSummary.

Edit this dict to add/remove themes. Matches are case-insensitive substring on the
business summary plus company name.
"""

THEMES: dict[str, list[str]] = {
    "AI": ["artificial intelligence", "machine learning", "large language model",
           "generative ai", "deep learning", "neural network", "foundation model"],
    "Semiconductor": ["semiconductor", "chipmaker", "wafer", "fabless", "foundry",
                      "integrated circuit", "asic", "gpu"],
    "Quantum": ["quantum computing", "quantum", "qubit"],
    "Space/Rocket": ["space launch", "rocket", "satellite", "spacecraft",
                     "low earth orbit", "aerospace propulsion"],
    "Defense": ["defense", "missile", "munitions", "military", "weapons system"],
    "Nuclear": ["nuclear reactor", "small modular reactor", "uranium", "smr"],
    "Biotech-Oncology": ["oncology", "tumor", "cancer therapy", "immuno-oncology"],
    "Biotech-Obesity": ["glp-1", "weight loss", "obesity", "anti-obesity"],
    "Biotech-GeneTherapy": ["gene therapy", "crispr", "mrna", "rna therapeutic"],
    "Crypto": ["bitcoin", "cryptocurrency", "blockchain", "digital asset",
               "crypto mining"],
    "Cybersecurity": ["cybersecurity", "endpoint security", "zero trust",
                      "threat detection"],
    "EV": ["electric vehicle", "ev charging", "battery electric", "lithium battery"],
    "Robotics": ["humanoid robot", "robotics", "autonomous robot"],
    "DataCenter": ["data center", "hyperscaler", "colocation"],
}


def tag_themes(business_summary: str, company_name: str = "") -> list[str]:
    if not business_summary:
        business_summary = ""
    haystack = (business_summary + " " + company_name).lower()
    hits = []
    for theme, kws in THEMES.items():
        if any(kw in haystack for kw in kws):
            hits.append(theme)
    return hits
